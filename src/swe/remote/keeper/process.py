"""The keeper: the process that holds one live session.

A :class:`Keeper` owns the agent's adapter (and through it the agent's
stdio), the send queue and the session's :class:`~.log.EventLog`, and
serves them to hubs over a Unix socket (:mod:`.ipc`). It is separate
from the hub so a hub restart, crash or upgrade leaves the agent
running, and one per session so a keeper's own crash touches one
session only.

Threads
=======

The command loop (:meth:`Keeper.run`, the process's main thread) is the
only thread that calls the adapter, as the adapter contract requires,
and the only one that changes the queue. Everything else hands it work
through one inbox:

- one reader and one writer thread per client connection;
- the accept thread;
- the adapter's own reader thread, which emits events into the log
  through :class:`_Sink` and leaves the loop a note when a turn ends or
  the agent goes down.

Log subscribers (one per client) run under the log's lock, so they only
append to the client's queue.

What it guarantees
==================

- A command is logged (``command.receipt``, fsynced) before it is
  acted on, and a ``command_id`` seen before answers ``duplicate``
  without being applied again. The ids are rebuilt from the log.
- A client joins without a gap: subscribe, then replay from disk up to
  the seq it subscribed at, then the events that arrived meanwhile.
- A client that stops reading is dropped, never waited for; it
  reconnects with its last seq and replays.
- An agent that dies mid-turn leaves ``turn.ended {failed}``, its open
  requests retired and the session ``agent_down``; the next send
  restarts it, at most ``restart_tries`` times in ``restart_window``.
- A keeper that died mid-turn is tidied by the next keeper of the
  session: open requests retired (``agent_exit``), the turn ended
  (``keeper_lost``), the queue rebuilt.
- An idle session is parked after ``park_after`` seconds: the agent is
  closed, the keeper stays, and the next send resumes it.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import os
import queue
import selectors
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import swe
from swe.remote.adapters import events as ev
from swe.remote.adapters import options as options_mod
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, SessionAdapter, StartSpec, TurnInput,
    Unsupported, check_option,
)
from swe.remote.keeper import discovery, ipc
from swe.remote.keeper.log import EventLog, current_boot_id
from swe.remote.protocol import key_slug
# The same redaction the adapters apply to message text before it is
# logged (see the adapter contract in adapters/base.py).
from swe.reports.transcripts import _redact_secrets

logger = logging.getLogger(__name__)

PARK_AFTER_S = 30 * 60.0
RESTART_TRIES = 3
RESTART_WINDOW_S = 10 * 60.0

# Frames one client may have waiting before it counts as stuck.
CLIENT_BUFFER = 8192
# How long one write to a client may block before it counts as stuck.
SEND_TIMEOUT_S = ipc.SEND_TIMEOUT_S
REPLAY_PAGE = 500
# Command ids remembered for ``duplicate`` answers.
RECENT_COMMANDS = 4096
# How far back a starting keeper reads its log for the queue and the
# recent command ids.
SCAN_BYTES = 64 << 20
MAX_ID = 200
# One message's text, as JSON. A frame is limited to ipc.MAX_LINE, and
# the whole queue travels in one frame (queue.updated, welcome.queue).
MAX_TEXT_BYTES = 1 << 20
MAX_ATTACHMENTS = 100
QUEUE_MAX_ITEMS = 100
QUEUE_MAX_BYTES = 4 << 20
# How long a starting keeper gives a dead keeper's orphaned agent to
# exit after SIGTERM before it kills it.
ORPHAN_GRACE_S = 2.0

# keeper.json's ``state``.
STARTING, RUNNING, PARKED, AGENT_DOWN, EXITED = (
    "starting", "running", "parked", "agent_down", "exited")

_CLOSE = object()


class _Sink:
    """The ``EventSink`` one adapter emits into: the log, plus a note to
    the keeper's loop for the events it acts on. One per adapter, so a
    note from an agent that is already replaced is recognised."""

    def __init__(self, keeper: "Keeper") -> None:
        self.keeper = keeper
        self.park_reason = ""
        self.parked = False

    def emit(self, event: dict) -> dict | None:
        etype = event.get("type") if isinstance(event, dict) else None
        data = event.get("data") if etype else None
        state = data.get("state") if etype == "session.state" \
            and isinstance(data, dict) else None
        if state == "closed" and self.park_reason:
            # Parking closes the agent, not the session.
            event = ev.session_state(self.keeper.session_key, "parked",
                                     reason=self.park_reason,
                                     ts=event.get("ts"))
            state = "parked"
            self.parked = True
        stamped = self.keeper.log.emit(event)
        if stamped is None:
            return None
        if etype == "session.options" and isinstance(data, dict) \
                and isinstance(data.get("options"), dict):
            # One reference swapped whole: welcomes read it from other
            # threads.
            self.keeper._shown_options = data["options"]
        if state is not None:
            self.keeper._note(self, state)
        elif etype == "turn.ended":
            self.keeper._note(self, "turn_ended")
        return stamped


class _Conn:
    """One client connection: a reader thread that feeds the keeper's
    inbox and a writer thread that owns every write to the socket."""

    def __init__(self, keeper: "Keeper", sock: socket.socket, n: int) -> None:
        self.keeper = keeper
        self.sock = sock
        self.n = n
        self.out: queue.SimpleQueue = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.live = False
        self.buffer: list[dict] = []
        self.queued = 0
        self.overflow = False
        self.closed = False
        self.sub: Any = None
        self.version = 0
        self.name = ""
        self.writer: threading.Thread | None = None
        self.reader = threading.Thread(
            target=self._read, daemon=True,
            name=f"swe-keeper-conn-{n}-read")

    # -- the log's callback (under the log's lock: enqueue and return) --

    def on_event(self, event: dict) -> None:
        with self.lock:
            if self.closed or self.overflow:
                return
            if not self.live:
                self.buffer.append(event)
                over = len(self.buffer) > self.keeper.client_buffer
            else:
                self.queued += 1
                over = self.queued > self.keeper.client_buffer
                if not over:
                    self.out.put(("event", event))
            if over:
                self.overflow = True
                self.buffer = []
                self.out.put(("overflow",))

    def post(self, frame: dict) -> None:
        """Queue one frame (a reply) for this client."""
        with self.lock:
            if self.closed or self.overflow:
                return
        self.out.put(("frame", frame))

    # -- reader --------------------------------------------------------

    def _read(self) -> None:
        keeper = self.keeper
        reader = ipc.LineReader(self.sock)
        try:
            try:
                hello = reader.read(keeper.hello_timeout)
            except (ipc.IpcError, TimeoutError):
                hello = None
            if hello is None or hello.get("t") != "hello":
                self._refuse("bad_hello")
                return
            version = ipc.negotiate(hello.get("v"), hello.get("min_v"),
                                    keeper.ipc_version, keeper.ipc_min)
            if version is None:
                self._refuse("version")
                return
            after = hello.get("last_seq")
            if after is not None and (not isinstance(after, int)
                                      or isinstance(after, bool)
                                      or after < 0):
                self._refuse("bad_hello")
                return
            self.version = version
            self.name = str(hello.get("client") or "")[:64]
            try:
                sub = keeper.log.subscribe(self.on_event)
            except ValueError:  # the log is closed: the keeper is going
                self._refuse("stopping")
                return
            self.sub = sub
            self.out.put(("frame", keeper._welcome(version, sub)))
            self.out.put(("replay", after, sub.last_seq))
            self.writer = threading.Thread(
                target=self._write, daemon=True,
                name=f"swe-keeper-conn-{self.n}-write")
            self.writer.start()
            while True:
                try:
                    frame = reader.read()
                except ipc.IpcError:
                    break
                if frame is None:
                    break
                if frame.get("t") != "cmd":
                    continue  # a frame type this version does not know
                if frame.get("op") == "ping":
                    self.post({"t": "reply", "id": frame.get("id"),
                               "ok": True, "op": "ping"})
                    continue
                keeper._inbox.put(("cmd", self, frame))
        finally:
            if self.writer is not None:
                self.out.put(_CLOSE)
            else:
                self._teardown()

    def _refuse(self, reason: str) -> None:
        keeper = self.keeper
        try:
            ipc.send_frame(self.sock, {
                "t": "refused", "reason": reason, "v": keeper.ipc_version,
                "min_v": keeper.ipc_min})
        except OSError:
            pass

    # -- writer --------------------------------------------------------

    def _write(self) -> None:
        try:
            while True:
                item = self.out.get()
                if item is _CLOSE:
                    return
                kind = item[0]
                if kind == "frame":
                    ipc.send_frame(self.sock, item[1])
                elif kind == "event":
                    ipc.send_frame(self.sock, {"t": "event", "e": item[1]})
                    with self.lock:
                        self.queued -= 1
                elif kind == "replay":
                    self._replay(item[1], item[2])
                elif kind == "bye":
                    ipc.send_frame(self.sock, {"t": "bye",
                                               "reason": item[1]})
                    return
                elif kind == "overflow":
                    ipc.send_frame(self.sock, {"t": "bye",
                                               "reason": "overflow"})
                    return
        except OSError:
            return  # the client is gone, or stuck past the send timeout
        finally:
            self._teardown()

    def _replay(self, after: int | None, until: int) -> None:
        log = self.keeper.log
        cursor = after
        while cursor is not None and cursor < until:
            try:
                page = log.replay(cursor, until_seq=until,
                                  limit=self.keeper.replay_page)
            except (OSError, ValueError):
                break
            if page.truncated:
                # On any page, not only the first: the cap can drop
                # segments this replay has not reached yet.
                ipc.send_frame(self.sock, {
                    "t": "gap", "oldest_seq": page.oldest_seq,
                    "reason": page.reason, "marker": page.marker})
            for event in page.events:
                ipc.send_frame(self.sock, {"t": "event", "e": event})
            if not page.more or not page.events:
                break
            cursor = page.events[-1]["seq"]
        ipc.send_frame(self.sock, {"t": "live", "last_seq": until})
        with self.lock:
            buffered, self.buffer = self.buffer, []
            self.live = True
            self.queued += len(buffered)
            for event in buffered:
                self.out.put(("event", event))

    def finish(self, reason: str) -> None:
        """Say goodbye and close once everything queued is written."""
        self.out.put(("bye", reason))

    def _teardown(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.buffer = []
        sub = self.sub
        if sub is not None:
            sub.close()
        # Forgotten before the socket closes: once the peer sees the
        # end of the stream, the keeper no longer counts it.
        self.keeper._forget(self)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class Keeper:
    """One session's keeper. :meth:`open` takes the session's lock,
    opens the log and the socket; :meth:`run` starts the agent and
    serves until a ``stop`` command or :meth:`stop`.

    ``adapter_factory`` makes a fresh adapter each time the agent is
    started (a closed adapter is never started again). ``clock`` is the
    monotonic clock for the idle and restart timers; ``wall`` stamps
    ``keeper.json``. ``log_options`` go to :class:`EventLog`.
    """

    def __init__(
        self,
        session_key: str,
        spec: StartSpec,
        adapter_factory: Callable[[], SessionAdapter],
        *,
        home: Path | str | None = None,
        runtime: Path | str | None = None,
        first_seq: int = 1,
        park_after: float = PARK_AFTER_S,
        restart_tries: int = RESTART_TRIES,
        restart_window: float = RESTART_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log_options: dict | None = None,
        ipc_version: int = ipc.VERSION,
        ipc_min: int = ipc.MIN_VERSION,
        client_buffer: int = CLIENT_BUFFER,
        replay_page: int = REPLAY_PAGE,
        send_timeout: float = SEND_TIMEOUT_S,
        hello_timeout: float = ipc.HELLO_TIMEOUT_S,
        token: str = "",
    ) -> None:
        if spec.session_key != session_key:
            raise ValueError("the spec is for another session")
        self.session_key = session_key
        self.slug = key_slug(session_key)
        self.spec = spec
        self.home = Path(home) if home is not None else None
        self.dir = discovery.keeper_dir_for(session_key, home)
        self.park_after = park_after
        self.restart_tries = restart_tries
        self.restart_window = restart_window
        self.ipc_version = ipc_version
        self.ipc_min = ipc_min
        self.client_buffer = client_buffer
        self.replay_page = replay_page
        self.send_timeout = send_timeout
        self.hello_timeout = hello_timeout
        self.socket_path: Path | None = None
        self.log: EventLog = None  # type: ignore[assignment]
        self._factory = adapter_factory
        self._runtime = runtime
        self._first_seq = first_seq
        self._clock = clock
        self._wall = wall
        self._log_options = dict(log_options or {})
        self._token = token
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._lock_fd: int | None = None
        self._listener: socket.socket | None = None
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._thread: threading.Thread | None = None
        self._conns: set[_Conn] = set()
        self._conns_lock = threading.Lock()
        self._conn_n = 0
        self._started_at = 0.0
        self._opened = False
        self._stopping = False
        self._stop_reason = ""
        self._done = threading.Event()
        # Owned by the command loop.
        self._adapter: SessionAdapter | None = None
        self._sink: _Sink | None = None
        self._agent = "none"  # none | up | down | parked
        self._queue: list[dict] = []
        self._seen: collections.OrderedDict[str, str] = \
            collections.OrderedDict()
        self._restarts: collections.deque[float] = collections.deque()
        self._idle_since: float | None = None
        self._harness_id = ""
        self._caps = Caps()
        self._options: dict[str, Any] = {}
        # The last ``session.options`` logged (G32): what a welcome and
        # ``status`` say. Replaced whole, never changed in place.
        self._shown_options: dict = {}
        self._transcript: dict | None = None
        self._log_errors = 0
        # Read by connection threads: the last session.state seen, and a
        # snapshot the loop replaces whole.
        self._state = "starting"
        self._public: dict = {"queue": [], "agent": "none", "caps":
                              Caps().as_dict(), "harness_session_id": ""}
        self._ops: dict[str, Callable[[_Conn, Any, str, dict], None]] = {
            "send": self._op_send,
            "interrupt": self._op_interrupt,
            "respond": self._op_respond,
            "set_option": self._op_set_option,
            "stop_task": self._op_stop_task,
            "rollback": self._op_rollback,
            "queue_edit": self._op_queue_edit,
            "queue_remove": self._op_queue_remove,
            "park": self._op_park,
            "stop": self._op_stop,
        }

    # -- opening -------------------------------------------------------

    def open(self, *, lock_tries: int = 10,
             sleep: Callable[[float], None] = time.sleep) -> None:
        """Take the session's lock, open the log and listen. Raises
        :class:`discovery.KeeperRunning` when another keeper holds the
        session, and ``ipc.Unsafe`` when the socket folder is not
        private."""
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        fd = None
        for attempt in range(max(1, lock_tries)):
            fd = discovery.take_lock(self.dir)
            if fd is not None:
                break
            # A hub probing the lock in discover() holds it for an
            # instant; a real keeper holds it for good.
            if attempt < lock_tries - 1:
                sleep(0.05)
        if fd is None:
            raise discovery.KeeperRunning(
                f"a keeper already holds {self.session_key}")
        self._lock_fd = fd
        try:
            previous = discovery.read_json(self.dir / discovery.KEEPER_FILE)
            if previous and isinstance(previous.get("harness_session_id"),
                                       str):
                self._harness_id = previous["harness_session_id"]
            boot = current_boot_id()
            if previous and boot and previous.get("boot") == boot:
                # Only within one boot: after a restart of the machine
                # the saved pid names nothing of ours, and could name a
                # stranger's process group.
                self._reap_orphans(previous.get("pid"), sleep)
            options = dict(self._log_options)
            options.setdefault("on_error", self._log_failed)
            self.log = EventLog(self.session_key, home=self.home,
                                first_seq=self._first_seq, **options)
            try:
                runtime = ipc.ensure_runtime_dir(self._runtime)
                path = ipc.socket_path(self.dir, runtime)
                try:
                    os.unlink(path)  # a dead keeper's; the lock is ours
                except FileNotFoundError:
                    pass
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    listener.bind(str(path))
                    os.chmod(path, 0o600)
                    listener.listen(16)
                except BaseException:
                    listener.close()
                    raise
            except BaseException:
                self.log.close()
                raise
        except BaseException:
            os.close(fd)
            self._lock_fd = None
            raise
        self._listener = listener
        self.socket_path = path
        self._wake_r, self._wake_w = socket.socketpair()
        self._started_at = self._wall()
        self._opened = True
        self._write_info(STARTING)
        self._accept_thread = threading.Thread(
            target=self._accept, daemon=True,
            name=f"swe-keeper-accept-{self.slug}")
        self._accept_thread.start()

    def _log_failed(self, exc: BaseException, event: dict | None) -> None:
        """The log could not write or fsync (a full disk, an I/O
        error). Counted, so a command whose receipt did not reach the
        disk is not acted on."""
        self._log_errors += 1
        logger.warning("keeper log %s: %s", self.dir, exc)

    def _reap_orphans(self, old_pid: Any,
                      sleep: Callable[[float], None] = time.sleep) -> None:
        """End what a dead keeper left running. An agent normally exits
        when its keeper dies (its stdin closes), but one that ignores
        that would run on beside the agent this keeper starts, both
        writing one session.

        A keeper is the leader of its own process group and its agent
        is a member. This keeper holds the session's lock, so the old
        keeper is gone; if its process group still has members while
        its leader does not exist, they are that keeper's orphans. (A
        pid cannot be handed out again while a group still carries it,
        so this cannot hit a stranger.)"""
        if not isinstance(old_pid, int) or isinstance(old_pid, bool) \
                or old_pid <= 1 or old_pid == os.getpid() \
                or old_pid == os.getpgrp():
            return
        try:
            os.kill(old_pid, 0)
            return  # a process has this pid: not ours to judge
        except ProcessLookupError:
            pass
        except OSError:
            return
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(old_pid, signum)
            except OSError:
                return  # no such group: nothing was left behind
            logger.warning("ending the agent a dead keeper (pid %d) left "
                           "running", old_pid)
            for _ in range(int(ORPHAN_GRACE_S / 0.05)):
                try:
                    os.killpg(old_pid, 0)
                except OSError:
                    return
                sleep(0.05)

    def _write_info(self, state: str) -> None:
        info = {
            "v": discovery.INFO_VERSION,
            "session": self.session_key,
            "slug": self.slug,
            "pid": os.getpid(),
            "socket": str(self.socket_path),
            "ipc": {"version": self.ipc_version, "min": self.ipc_min},
            "state": state,
            "harness": self.spec.harness,
            "harness_session_id": self._harness_id,
            "started_at": self._started_at,
            "updated_at": self._wall(),
            "boot": current_boot_id(),
            "swe_version": getattr(swe, "__version__", ""),
            "token": self._token,
        }
        if self._transcript:
            info["transcript"] = self._transcript
        try:
            discovery.write_json(self.dir / discovery.KEEPER_FILE, info)
        except OSError:
            logger.exception("could not write keeper.json")

    # -- connections ---------------------------------------------------

    def _accept(self) -> None:
        listener, wake = self._listener, self._wake_r
        assert listener is not None and wake is not None
        sel = selectors.DefaultSelector()
        sel.register(listener, selectors.EVENT_READ)
        sel.register(wake, selectors.EVENT_READ)
        try:
            while True:
                for key, _ in sel.select():
                    if key.fileobj is wake:
                        return
                    try:
                        sock, _ = listener.accept()
                    except OSError:
                        return
                    uid = ipc.peer_uid(sock)
                    if uid is not None and uid != os.getuid():
                        sock.close()
                        continue
                    try:
                        ipc.set_send_timeout(sock, self.send_timeout)
                    except OSError:
                        pass
                    with self._conns_lock:
                        self._conn_n += 1
                        conn = _Conn(self, sock, self._conn_n)
                        self._conns.add(conn)
                    conn.reader.start()
        finally:
            sel.close()

    def _forget(self, conn: _Conn) -> None:
        with self._conns_lock:
            self._conns.discard(conn)

    def _welcome(self, version: int, sub: Any) -> dict:
        public = self._public
        return {
            "t": "welcome", "v": version,
            "keeper_v": self.ipc_version, "keeper_min_v": self.ipc_min,
            "session": self.session_key, "pid": os.getpid(),
            "last_seq": sub.last_seq,
            "first_seq": self.log.first_seq,
            "oldest_seq": self.log.oldest_seq,
            "pending": sub.pending, "live": sub.live,
            "open_turn": sub.open_turn,
            "state": self._state, "agent": public["agent"],
            "queue": public["queue"], "caps": public["caps"],
            "options": self._shown_options,
            "harness_session_id": public["harness_session_id"],
        }

    # -- the loop ------------------------------------------------------

    def run(self) -> None:
        """Start the agent and serve commands until stopped. Call from
        the thread that will own the adapter."""
        if not self._opened:
            raise RuntimeError("open() the keeper first")
        try:
            self._rebuild()
            self._retire_stale()
            self._start_agent(counted=False)
            self._drain()
            while not self._stopping:
                try:
                    self._check_idle()
                except Exception:  # a failed park must not end the keeper
                    logger.exception("keeper idle check failed")
                    self._idle_since = None
                try:
                    item = self._inbox.get(timeout=self._wait_s())
                except queue.Empty:
                    continue
                self._handle(item)
        finally:
            self._shutdown()

    def start(self) -> "Keeper":
        """:meth:`open`, then :meth:`run` on a thread of its own."""
        self.open()
        self._thread = threading.Thread(
            target=self.run, daemon=True, name=f"swe-keeper-{self.slug}")
        self._thread.start()
        return self

    def stop(self, reason: str = "stopped", timeout: float = 15.0) -> None:
        """Ask the loop to stop and wait for it. Safe from any thread
        and from a signal handler (it only posts to the inbox)."""
        self._inbox.put(("stop", reason))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def request_stop(self, reason: str = "signal") -> None:
        """Post a stop without waiting (for a signal handler)."""
        self._inbox.put(("stop", reason))

    def poke(self) -> None:
        """Wake the loop to look at its timers again (after a test
        moved the clock)."""
        self._inbox.put(("poke",))

    def sync(self, timeout: float = 10.0) -> bool:
        """Wait until the loop has handled everything posted so far."""
        done = threading.Event()
        self._inbox.put(("sync", done))
        return done.wait(timeout)

    @property
    def done(self) -> bool:
        """True once the loop has shut everything down."""
        return self._done.is_set()

    def _wait_s(self) -> float | None:
        if self._idle_since is None or self._agent != "up":
            return None
        left = self.park_after - (self._clock() - self._idle_since)
        return max(0.05, left)

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "cmd":
            try:
                self._command(item[1], item[2])
            except Exception:  # one bad command must not end the keeper
                logger.exception("keeper command failed")
                item[1].post({"t": "reply", "id": item[2].get("id"),
                              "status": "rejected",
                              "reason": "internal_error"})
        elif kind == "note":
            try:
                self._on_note(item[1], item[2])
            except Exception:
                logger.exception("keeper note %s failed", item[2])
        elif kind == "sync":
            item[1].set()
        elif kind == "stop":
            self._stopping = True
            self._stop_reason = item[1]

    def _note(self, sink: _Sink, what: str) -> None:
        """From the adapter's thread: a state it reported, or a turn's
        end."""
        if what != "turn_ended":
            self._state = what
        self._inbox.put(("note", sink, what))

    def _on_note(self, sink: _Sink, what: str) -> None:
        if sink is not self._sink:
            return  # from an agent that has already been replaced
        self._idle_since = None
        if what == "agent_down":
            self._agent_lost()
        elif what in ("turn_ended", "idle"):
            self._drain()

    def _emit(self, event: dict) -> dict | None:
        """Log one of the keeper's own events."""
        if event["type"] == "session.state":
            self._state = event["data"]["state"]
        stamped = self.log.emit(event)
        if stamped is not None and event["type"] == "session.options":
            self._shown_options = event["data"]["options"]
        return stamped

    def _shown_queue(self) -> list[dict]:
        """The queue as events and clients see it: message text
        redacted, as the adapter redacts a message it sends. The text
        as typed stays in memory only."""
        return [dict(i, text=_redact_secrets(i["text"]))
                for i in self._queue]

    def _publish(self) -> None:
        self._public = {
            "queue": self._shown_queue(),
            "agent": self._agent,
            "caps": self._caps.as_dict(),
            "harness_session_id": self._harness_id,
        }

    # -- starting up ---------------------------------------------------

    def _rebuild(self) -> None:
        """The queue, the recent command ids and the harness's session
        id, read back from the log."""
        queue_found = False
        options_found = False
        harness_id = ""
        for rec in self.log.read_back(max_bytes=SCAN_BYTES):
            etype = rec.get("type")
            data = rec.get("data")
            if not isinstance(data, dict):
                continue
            if etype == "command.receipt":
                cid = rec.get("command_id")
                if isinstance(cid, str) and cid not in self._seen \
                        and len(self._seen) < RECENT_COMMANDS:
                    self._seen[cid] = str(data.get("status") or "accepted")
            elif etype == "queue.updated" and not queue_found:
                queue_found = True
                items = data.get("items")
                self._queue = [dict(i) for i in items
                               if _queue_item_ok(i)] \
                    if isinstance(items, list) else []
            elif etype == "session.state" and not harness_id:
                hid = data.get("harness_session_id")
                if isinstance(hid, str) and hid:
                    harness_id = hid
            elif etype == "session.options" and not options_found:
                options_found = True
                if isinstance(data.get("options"), dict):
                    self._shown_options = data["options"]
            if queue_found and harness_id and options_found \
                    and len(self._seen) >= RECENT_COMMANDS:
                break
        # read_back is newest first; duplicates are answered oldest
        # dropped first.
        self._seen = collections.OrderedDict(reversed(self._seen.items()))
        # A message can be in the queue without its receipt (the queue
        # is written first): it is still a command already taken.
        for item in self._queue:
            self._seen.setdefault(item["command_id"], "queued")
        if harness_id:
            self._harness_id = harness_id
        self._publish()

    def _retire_stale(self) -> None:
        """A keeper that died took its agent with it: close what the log
        still shows open."""
        recovery = self.log.recovery
        for rec in recovery.pending:
            data = rec.get("data") if isinstance(rec, dict) else None
            rid = data.get("request_id") if isinstance(data, dict) else None
            if isinstance(rid, str) and rid:
                self._emit(ev.approval_resolved(
                    self.session_key, rid, "agent_exit",
                    turn_id=rec.get("turn_id")))
        if recovery.open_turn:
            self._emit(ev.turn_ended(
                self.session_key, recovery.open_turn, "keeper_lost",
                reason="the keeper stopped while this turn was running"))

    def _start_spec(self) -> StartSpec:
        changes: dict[str, Any] = dict(self._options)
        if self._harness_id:
            # Every start after the first resumes the same session.
            changes.update(resume_id=self._harness_id, fork=False)
        return dataclasses.replace(self.spec, **changes)

    def _start_agent(self, *, counted: bool) -> tuple[bool, str]:
        """Start a fresh adapter. ``counted`` starts are restarts after
        a crash and are limited to ``restart_tries`` in
        ``restart_window``."""
        if counted:
            now = self._clock()
            while self._restarts and \
                    now - self._restarts[0] >= self.restart_window:
                self._restarts.popleft()
            if len(self._restarts) >= self.restart_tries:
                left = self.restart_window - (now - self._restarts[0])
                return False, (
                    f"the agent stopped {self.restart_tries} times in "
                    f"{int(self.restart_window // 60)} minutes; try again "
                    f"in {max(1, int(-(-left // 60)))} min")
            self._restarts.append(now)
        sink = _Sink(self)
        adapter = None
        try:
            adapter = self._factory()
            self._sink = sink
            harness_id = adapter.start(self._start_spec(), sink)
        except Exception as exc:
            code = exc.code if isinstance(exc, AdapterError) else "start_failed"
            if not isinstance(exc, (AdapterError, ValueError)):
                logger.exception("the agent did not start")
            self._sink = None
            if adapter is not None:
                try:
                    adapter.close(0)
                except Exception:
                    logger.exception("closing a failed adapter")
            message = str(exc) or code
            self._emit(ev.error(self.session_key, code, message))
            if self._state != "agent_down":  # the adapter may have said so
                self._emit(ev.session_state(self.session_key, "agent_down",
                                            reason=message))
            self._agent = "down"
            self._publish()
            self._write_info(AGENT_DOWN)
            return False, message
        self._adapter = adapter
        self._agent = "up"
        self._idle_since = None
        self._transcript = None
        if isinstance(harness_id, str) and harness_id:
            self._harness_id = harness_id
        self._caps = adapter.capabilities
        self._drop_unoffered()
        self._publish()
        self._write_info(RUNNING)
        return True, ""

    def _drop_unoffered(self) -> None:
        """An agent started with narrower caps than the one before it
        (an older CLI after a restart): options it cannot change are no
        longer offered. The adapter says only what it offers, and says
        nothing when that is nothing, so the keeper says the rest."""
        shown = options_mod.allowed(self._shown_options, self._caps)
        if shown != self._shown_options:
            self._emit(ev.session_options(self.session_key, shown))

    def _wake_agent(self) -> tuple[bool, str]:
        """Bring the agent back for a send: a restart after a crash
        (counted), or a resume from parked (not counted)."""
        if self._agent == "up":
            return True, ""
        return self._start_agent(counted=self._agent == "down")

    def _agent_lost(self) -> None:
        adapter, self._adapter, self._sink = self._adapter, None, None
        if adapter is not None:
            self._caps = _caps_of(adapter, self._caps)
            try:
                adapter.close(0)
            except Exception:
                logger.exception("closing a dead adapter")
        self._agent = "down"
        self._idle_since = None
        self._publish()
        self._write_info(AGENT_DOWN)

    # -- idle park -----------------------------------------------------

    def _check_idle(self) -> None:
        if self._agent != "up" or self._queue or self._state != "idle":
            self._idle_since = None
            return
        snap = self.log.snapshot()
        if snap.open_turn is not None or snap.pending:
            self._idle_since = None
            return
        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= self.park_after:
            self._park(f"idle for {_minutes(self.park_after)}")

    def _park(self, reason: str) -> None:
        """Close the agent and keep the session: the next send resumes
        it by the harness's own id."""
        adapter, sink = self._adapter, self._sink
        assert adapter is not None and sink is not None
        try:
            position = adapter.transcript_position()
        except Exception:
            position = None
        self._transcript = {"path": position.path,
                            "offset": position.offset} if position else None
        self._caps = _caps_of(adapter, self._caps)
        sink.park_reason = reason
        try:
            adapter.close()
        except Exception:
            logger.exception("closing the agent to park it")
        self._adapter, self._sink = None, None
        if not sink.parked:
            self._emit(ev.session_state(self.session_key, "parked",
                                        reason=reason))
        self._agent = "parked"
        self._idle_since = None
        self._publish()
        self._write_info(PARKED)

    # -- commands ------------------------------------------------------

    def _command(self, conn: _Conn, frame: dict) -> None:
        op = frame.get("op")
        rpc = frame.get("id")
        if op == "status":
            conn.post({"t": "reply", "id": rpc, "ok": True, "op": op,
                       **self.status()})
            return
        if op == "has_command":
            cid = frame.get("command_id")
            status = self._seen.get(cid) if isinstance(cid, str) else None
            conn.post({"t": "reply", "id": rpc, "ok": True, "op": op,
                       "known": status is not None, "status": status})
            return
        # Anything past a question is activity: the idle count restarts.
        self._idle_since = None
        cid = frame.get("command_id")
        if not isinstance(cid, str) or not cid or len(cid) > MAX_ID:
            conn.post({"t": "reply", "id": rpc, "status": "rejected",
                       "reason": "a command needs a command_id",
                       "op": op if isinstance(op, str) else None})
            return
        first = self._seen.get(cid)
        if first is not None:
            conn.post({"t": "reply", "id": rpc, "status": "duplicate",
                       "first": first, "op": op, "command_id": cid})
            return
        handler = self._ops.get(op) if isinstance(op, str) else None
        if handler is None:
            self._receipt(conn, rpc, cid, str(op)[:64] or "?", "rejected",
                          "unknown_op")
            return
        handler(conn, rpc, cid, frame)

    def _receipt(self, conn: _Conn, rpc: Any, cid: str, op: str,
                 status: str, reason: str | None = None) -> bool:
        """Record the answer to a command (durably, before it is acted
        on) and send it back. False when the log could not keep the
        receipt (a full disk, an I/O error): the command is then
        answered ``rejected`` and must not be applied, or a retry after
        a restart would apply it a second time."""
        errors = self._log_errors
        stamped = self._emit(ev.command_receipt(self.session_key, cid,
                                                status, reason=reason, op=op))
        if stamped is None or self._log_errors != errors:
            if stamped is not None and status != "rejected":
                # Written but perhaps not on disk. A later keeper may
                # or may not find that receipt, so the command is
                # refused for good under this id: recorded as rejected
                # (best effort) and remembered as such.
                self._seen[cid] = "rejected"
                self._emit(ev.command_receipt(
                    self.session_key, cid, "rejected",
                    reason="not_recorded", op=op))
            conn.post({"t": "reply", "id": rpc, "status": "rejected",
                       "reason": "not_recorded", "op": op,
                       "command_id": cid})
            return False
        self._seen[cid] = status
        while len(self._seen) > RECENT_COMMANDS:
            self._seen.popitem(last=False)
        reply = {"t": "reply", "id": rpc, "status": status, "op": op,
                 "command_id": cid}
        if reason:
            reply["reason"] = reason
        conn.post(reply)
        return True

    def _result(self, cid: str, op: str, ok: bool, *,
                note: str | None = None, cancelled: bool | None = None,
                request_id: str | None = None) -> None:
        self._emit(ev.command_result(self.session_key, cid, op, ok,
                                     note=note or None, cancelled=cancelled,
                                     request_id=request_id))

    def _call(self, cid: str, op: str, fn: Callable[[], Any], *,
              request_id: str | None = None,
              then: Callable[[], Any] | None = None) -> bool:
        """Run one adapter call for an accepted command and record how
        it went. ``then`` runs after a call that worked and before its
        result is recorded, so whoever sees the result sees its effects
        too."""
        try:
            fn()
        except AdapterError as exc:
            self._result(cid, op, False, note=str(exc),
                         request_id=request_id)
            if exc.code == "agent_down":
                self._agent_lost()
            return False
        except ValueError as exc:
            self._result(cid, op, False, note=str(exc),
                         request_id=request_id)
            return False
        if then is not None:
            then()
        self._result(cid, op, True, request_id=request_id)
        return True

    def _queue_changed(self) -> None:
        self._publish()
        self._emit(ev.queue_updated(self.session_key, self._shown_queue()))

    def _op_send(self, conn: _Conn, rpc: Any, cid: str, frame: dict) -> None:
        text = frame.get("text")
        attachments = frame.get("attachments") or []
        device = frame.get("device") or ""
        mode = frame.get("mode") or "queue"
        if not isinstance(text, str) or not text.strip() \
                or not isinstance(attachments, list) \
                or not all(isinstance(a, str) and a for a in attachments) \
                or not isinstance(device, str) \
                or mode not in ev.QUEUE_MODES:
            self._receipt(conn, rpc, cid, "send", "rejected", "bad_request")
            return
        if len(attachments) > MAX_ATTACHMENTS or len(ipc.encode(
                {"text": text, "attachments": attachments})) > MAX_TEXT_BYTES:
            self._receipt(conn, rpc, cid, "send", "rejected", "too_large")
            return
        item: dict[str, Any] = {"command_id": cid, "text": text}
        if device:
            item["by"] = device
        if attachments:
            item["attachments"] = list(attachments)
        ok, why = self._wake_agent()
        if not ok:
            self._receipt(conn, rpc, cid, "send", "rejected", why)
            return
        busy = self.log.snapshot().open_turn is not None
        if busy and mode == "steer" and self._caps.steer:
            if not self._receipt(conn, rpc, cid, "send", "accepted"):
                return
            adapter = self._adapter
            assert adapter is not None
            self._call(cid, "send", lambda: adapter.steer(_turn(item)))
            return
        if busy or self._queue:
            item["mode"] = "queue"
            if len(self._queue) >= QUEUE_MAX_ITEMS or len(ipc.encode(
                    {"items": self._queue + [item]})) > QUEUE_MAX_BYTES:
                self._receipt(conn, rpc, cid, "send", "rejected",
                              "queue_full")
                return
            # The queue is written before the receipt, and the
            # receipt's fsync carries both to disk: a crash between the
            # two leaves a queued message with no receipt (rebuilt, and
            # counted as taken), never a receipt with no message.
            self._queue.append(item)
            self._queue_changed()
            if not self._receipt(conn, rpc, cid, "send", "queued"):
                self._queue.remove(item)
                self._queue_changed()
                return
            self._drain()
            return
        if self._receipt(conn, rpc, cid, "send", "accepted"):
            self._send_now(item)

    def _send_now(self, item: dict) -> bool:
        """Start a turn with ``item``. False when it could not start; a
        turn already running (one the agent began by itself) puts the
        item back at the head of the queue."""
        adapter = self._adapter
        assert adapter is not None
        cid = item["command_id"]
        try:
            adapter.send_turn(_turn(item))
        except AdapterError as exc:
            if exc.code == "busy":
                self._queue.insert(0, dict(item, mode="queue"))
                self._queue_changed()
                return False
            if exc.code == "agent_down":
                # The agent died before it took the message: it waits
                # at the head of the queue for the restart.
                self._queue.insert(0, dict(item, mode="queue"))
                self._queue_changed()
                self._agent_lost()
                return False
            self._result(cid, "send", False, note=str(exc))
            return False
        except ValueError as exc:
            self._result(cid, "send", False, note=str(exc))
            return False
        self._result(cid, "send", True)
        return True

    def _drain(self) -> None:
        """Send queued messages, oldest first, while no turn is open."""
        while self._queue and self._agent == "up" \
                and self._state != "agent_down" \
                and self.log.snapshot().open_turn is None:
            item = self._queue.pop(0)
            self._queue_changed()
            if not self._send_now(item):
                break

    def _need_agent(self, conn: _Conn, rpc: Any, cid: str, op: str) -> bool:
        if self._agent == "up":
            return True
        self._receipt(conn, rpc, cid, op, "rejected",
                      "parked" if self._agent == "parked" else "agent_down")
        return False

    def _op_interrupt(self, conn: _Conn, rpc: Any, cid: str,
                      frame: dict) -> None:
        if not self._need_agent(conn, rpc, cid, "interrupt"):
            return
        if not self._receipt(conn, rpc, cid, "interrupt", "accepted"):
            return
        if frame.get("clear_queue") is True and self._queue:
            dropped, self._queue = self._queue, []
            self._queue_changed()
            for item in dropped:
                self._result(item["command_id"], "send", False,
                             cancelled=True, note="removed from the queue")
        adapter = self._adapter
        assert adapter is not None
        self._call(cid, "interrupt", adapter.interrupt)

    def _op_respond(self, conn: _Conn, rpc: Any, cid: str,
                    frame: dict) -> None:
        if not self._need_agent(conn, rpc, cid, "respond"):
            return
        rid = frame.get("request_id")
        asked = {e["data"]["request_id"]: e
                 for e in self.log.snapshot().pending}
        open_ids = set(asked)
        if not isinstance(rid, str) or rid not in open_ids:
            # Never asked, or someone else answered first.
            self._receipt(conn, rpc, cid, "respond", "rejected",
                          "unknown_request")
            return
        try:
            answer = Answer(decision=_str(frame.get("decision")),
                            option_id=_str(frame.get("option_id")),
                            text=_str(frame.get("text")),
                            device=_str(frame.get("device")))
        except ValueError as exc:
            self._receipt(conn, rpc, cid, "respond", "rejected", str(exc))
            return
        if not self._receipt(conn, rpc, cid, "respond", "accepted"):
            return
        adapter = self._adapter
        assert adapter is not None
        then = None
        if asked[rid]["data"].get("kind") == "plan" \
                and answer.decision in ("allow", "always"):
            # An approved plan ends plan mode; a later start must not
            # put the agent back in it.
            then = lambda: self._remember("plan_mode", False)  # noqa: E731
        self._call(cid, "respond", lambda: adapter.respond(rid, answer),
                   request_id=rid, then=then)

    def _op_set_option(self, conn: _Conn, rpc: Any, cid: str,
                       frame: dict) -> None:
        name, value = frame.get("name"), frame.get("value")
        try:
            check_option(self._caps, name, value)
        except Unsupported:
            self._receipt(conn, rpc, cid, "set_option", "rejected",
                          "unsupported")
            return
        except (ValueError, TypeError) as exc:
            self._receipt(conn, rpc, cid, "set_option", "rejected",
                          str(exc))
            return
        if not self._receipt(conn, rpc, cid, "set_option", "accepted"):
            return
        adapter = self._adapter
        if adapter is None:
            # No agent now: the next start carries it.
            self._remember(name, value)
            self._show_remembered(name, value)
            self._result(cid, "set_option", True,
                         note="applies when the agent starts")
            return
        self._call(cid, "set_option",
                   lambda: adapter.set_option(name, value),
                   then=lambda: self._remember(name, value))

    def _remember(self, name: str, value: Any) -> None:
        """Keep an option that took effect, for every later start of
        the agent: in memory, and in ``spec.json`` so the next keeper
        of the session starts the agent the same way.

        Plan mode is a mode like the permission modes: choosing a
        permission mode leaves plan mode, so the last change of the two
        is the one a restart applies."""
        changes = {name: value}
        if name == "permission_mode" and (
                self._options.get("plan_mode") or self.spec.plan_mode):
            changes["plan_mode"] = False
        if all(self._options.get(k, getattr(self.spec, k)) == v
               for k, v in changes.items()):
            return
        self._options.update(changes)
        try:
            discovery.update_spec(self.session_key, self.home, **changes)
        except OSError:
            logger.exception("could not record %s in spec.json", name)

    def _show_remembered(self, name: str, value: Any) -> None:
        """With no agent to ask, ``session.options`` says the value the
        next start will use (the start then says what the agent made of
        it). Choosing a permission mode leaves plan mode, as in
        ``_remember``."""
        shown = {k: dict(v) for k, v in self._shown_options.items()
                 if isinstance(v, dict)}
        if name not in shown:
            return
        changes = {name: value}
        if name == "permission_mode" and "plan_mode" in shown:
            changes["plan_mode"] = False
        for key, new in changes.items():
            shown[key]["value"] = new
            shown[key].pop("reported", None)
        if shown != self._shown_options:
            self._emit(ev.session_options(self.session_key, shown))

    def _op_stop_task(self, conn: _Conn, rpc: Any, cid: str,
                      frame: dict) -> None:
        self._gated(conn, rpc, cid, "stop_task", "stop_task",
                    frame.get("task_id"),
                    lambda a, v: a.stop_task(v))

    def _op_rollback(self, conn: _Conn, rpc: Any, cid: str,
                     frame: dict) -> None:
        self._gated(conn, rpc, cid, "rollback", "rollback",
                    frame.get("turn_id"),
                    lambda a, v: a.rollback(v))

    def _gated(self, conn: _Conn, rpc: Any, cid: str, op: str, feature: str,
               value: Any, call: Callable[[SessionAdapter, str], Any]) -> None:
        if not self._need_agent(conn, rpc, cid, op):
            return
        if not self._caps.supports(feature):
            self._receipt(conn, rpc, cid, op, "rejected", "unsupported")
            return
        if not isinstance(value, str) or not value:
            self._receipt(conn, rpc, cid, op, "rejected", "bad_request")
            return
        if not self._receipt(conn, rpc, cid, op, "accepted"):
            return
        adapter = self._adapter
        assert adapter is not None
        self._call(cid, op, lambda: call(adapter, value))

    def _queued(self, target: Any) -> int:
        for i, item in enumerate(self._queue):
            if item["command_id"] == target:
                return i
        return -1

    def _op_queue_edit(self, conn: _Conn, rpc: Any, cid: str,
                       frame: dict) -> None:
        at = self._queued(frame.get("target"))
        text = frame.get("text")
        if at < 0:
            self._receipt(conn, rpc, cid, "queue_edit", "rejected",
                          "not_queued")
            return
        if not isinstance(text, str) or not text.strip():
            self._receipt(conn, rpc, cid, "queue_edit", "rejected",
                          "bad_request")
            return
        before = self._queue[at]
        edited = self._queue[:at] + [dict(before, text=text)] \
            + self._queue[at + 1:]
        if len(ipc.encode({"text": text})) > MAX_TEXT_BYTES \
                or len(ipc.encode({"items": edited})) > QUEUE_MAX_BYTES:
            self._receipt(conn, rpc, cid, "queue_edit", "rejected",
                          "too_large")
            return
        # As for a queued send: the change first, then the receipt
        # whose fsync carries both.
        self._queue = edited
        self._queue_changed()
        if not self._receipt(conn, rpc, cid, "queue_edit", "accepted"):
            self._queue[at] = before
            self._queue_changed()
            return
        self._result(cid, "queue_edit", True)

    def _op_queue_remove(self, conn: _Conn, rpc: Any, cid: str,
                         frame: dict) -> None:
        at = self._queued(frame.get("target"))
        if at < 0:
            self._receipt(conn, rpc, cid, "queue_remove", "rejected",
                          "not_queued")
            return
        item = self._queue.pop(at)
        self._queue_changed()
        if not self._receipt(conn, rpc, cid, "queue_remove", "accepted"):
            self._queue.insert(at, item)
            self._queue_changed()
            return
        self._result(item["command_id"], "send", False, cancelled=True,
                     note="removed from the queue")
        self._result(cid, "queue_remove", True)

    def _op_park(self, conn: _Conn, rpc: Any, cid: str, frame: dict) -> None:
        if not self._need_agent(conn, rpc, cid, "park"):
            return
        if self.log.snapshot().open_turn is not None or self._queue:
            self._receipt(conn, rpc, cid, "park", "rejected", "busy")
            return
        if not self._receipt(conn, rpc, cid, "park", "accepted"):
            return
        self._park("parked on request")
        self._result(cid, "park", True)

    def _op_stop(self, conn: _Conn, rpc: Any, cid: str, frame: dict) -> None:
        # A stop is honoured even when the disk refuses the receipt: it
        # is how a keeper in trouble is ended.
        self._receipt(conn, rpc, cid, "stop", "accepted")
        self._result(cid, "stop", True)
        self._stopping = True
        self._stop_reason = "stopped"

    def status(self) -> dict:
        """What the ``status`` op answers (called on the loop)."""
        snap = self.log.snapshot()
        recovery = self.log.recovery
        now = self._clock()
        used = sum(1 for t in self._restarts
                   if now - t < self.restart_window)
        with self._conns_lock:
            clients = len(self._conns)
        return {
            "session": self.session_key, "pid": os.getpid(),
            "state": self._state, "agent": self._agent,
            "last_seq": snap.last_seq, "first_seq": self.log.first_seq,
            "oldest_seq": self.log.oldest_seq,
            "open_turn": snap.open_turn,
            "pending": [e["data"]["request_id"] for e in snap.pending],
            "queue": self._shown_queue(),
            "caps": self._caps.as_dict(),
            "options": self._shown_options,
            "harness_session_id": self._harness_id,
            "clients": clients,
            "restarts_left": max(0, self.restart_tries - used),
            "transcript": self._transcript,
            "recovery": {"torn_bytes": recovery.torn_bytes,
                         "skipped_lines": recovery.skipped_lines,
                         "seq_gap": recovery.seq_gap},
            "log": self.log.stats(),
            "marker": self.log.marker,
        }

    # -- stopping ------------------------------------------------------

    def _shutdown(self) -> None:
        """Close the agent, then the log (an emit after the log closes
        is dropped), then let go of the socket and the lock."""
        reason = self._stop_reason or "stopped"
        try:
            listener, self._listener = self._listener, None
            if self._wake_w is not None:
                try:
                    self._wake_w.send(b"x")
                except OSError:
                    pass
            if self._accept_thread is not None:
                self._accept_thread.join(5.0)
            if listener is not None:
                listener.close()
            for sock in (self._wake_r, self._wake_w):
                if sock is not None:
                    sock.close()
            if self.socket_path is not None:
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
            adapter, self._adapter = self._adapter, None
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    logger.exception("closing the agent")
            self._agent = "none"
            with self._conns_lock:
                conns = list(self._conns)
            for conn in conns:
                conn.finish(reason)
            for conn in conns:
                if conn.writer is not None:
                    conn.writer.join(5.0)
            self.log.close()
            self._write_info(EXITED)
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)  # drops the flock
                self._lock_fd = None
            self._done.set()


# ---------------------------------------------------------------------


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _turn(item: dict) -> TurnInput:
    return TurnInput(item["text"], attachments=tuple(
        item.get("attachments") or ()), command_id=item["command_id"],
        device=item.get("by") or "")


def _queue_item_ok(item: Any) -> bool:
    return isinstance(item, dict) \
        and isinstance(item.get("command_id"), str) and item["command_id"] \
        and isinstance(item.get("text"), str)


def _caps_of(adapter: SessionAdapter, default: Caps) -> Caps:
    try:
        caps = adapter.capabilities
    except Exception:
        return default
    return caps if isinstance(caps, Caps) else default


def _minutes(seconds: float) -> str:
    whole = int(round(seconds / 60))
    if whole >= 1 and abs(seconds - whole * 60) < 1:
        return f"{whole} minute{'s' if whole != 1 else ''}"
    return f"{seconds:g} seconds"
