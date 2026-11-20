"""The Claude Code session adapter: one long-lived stream-json process.

``ClaudeStreamAdapter`` starts ``claude`` with the spawn line from
:func:`claude_wire.spawn_argv` (the base argv from harness.json's
``capabilities.session.argv``, or the fallback, plus the protocol flags
and this session's own arguments), sends the ``initialize`` control
request, and from then on reads the CLI's stdout on its own thread,
turning every line into v2 events through
:class:`claude_wire.StreamState`. Everything about the wire format is in
``claude_wire.py``; this module owns the process, the threads, the
locks and the bounded waits, under the contract in ``base.py``.

- ``start`` blocks on the handshake (``INIT_TIMEOUT_S``), checks the
  CLI version against ``min_version``, and returns the session id it
  passed with ``--session-id`` (a resume keeps the resumed id).
- ``send_turn`` writes one user message and returns; the turn ends when
  the CLI reports ``session_state_changed: idle``.
- ``steer`` writes one user message with ``priority: "now"`` into the
  open turn: the CLI cuts the reply in progress (a running tool call is
  killed, an open approval withdrawn) and answers it in the same turn.
- ``interrupt``, ``set_option`` and ``stop_task`` send a control
  request and return once the CLI acknowledges it (``CALL_TIMEOUT_S``).
- ``respond`` writes the ``control_response`` to an open
  ``can_use_tool``.
- ``close`` retires what is open, ends the turn, emits ``closed``, then
  closes stdin (the CLI drops pending prompts on EOF), and terminates
  and kills it if it lingers.
"""

from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from swe.remote.adapters import claude_wire as wire
from swe.remote.adapters import registry
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, EventSink, StartSpec, TranscriptPosition,
    TurnInput, Unsupported, check_option,
)

# How long the handshake, a control request, one stdin write and the
# kill steps get.
INIT_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 15.0
WRITE_TIMEOUT_S = 10.0
_KILL_WAIT_S = 3.0

# Lines of the CLI's stderr kept to explain a failed start or an exit.
_STDERR_LINES = 20


class _Waiter:
    __slots__ = ("event", "ack")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.ack: wire.Ack | None = None


class _Stalled(Exception):
    """A stdin write did not reach the pipe in time: the CLI stopped
    reading."""


class _Writer:
    """The one thread that writes the CLI's stdin.

    A pipe write blocks while the pipe is full, so no caller ever writes
    itself: ``send`` queues the bytes and waits, bounded, for them to
    reach the pipe; the reader thread's own replies are queued without
    waiting, so a CLI that stops reading can never stall the reader.
    Once a write misses its deadline the writer counts as stalled and
    takes nothing more; the adapter then kills the child, which ends
    the blocked write."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.Queue = queue.Queue()
        self.broken = False
        self.stalled = False
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="claude-stream-stdin")
        self.thread.start()

    def send(self, data: bytes | None, timeout: float | None) -> bool:
        """Queue ``data`` (None: close stdin). With a ``timeout``, wait
        that long for it to be written: True when it was, False when the
        pipe is gone; raises ``_Stalled`` past the deadline. Without
        one, True once queued."""
        if self.broken or self.stalled:
            return False
        done = threading.Event()
        result = [False]
        self._queue.put((data, done, result))
        if timeout is None:
            return True
        if not done.wait(timeout):
            self.stalled = True
            raise _Stalled()
        return result[0]

    def _run(self) -> None:
        """Write until stdin is closed on purpose or the pipe breaks (the
        child exited, or was killed under a stalled write); either way
        the stream is closed and the thread ends, failing whatever was
        still queued."""
        while True:
            data, done, result = self._queue.get()
            if data is None:
                result[0] = True
                break
            try:
                self._stream.write(data)
                self._stream.flush()
                result[0] = True
            except (OSError, ValueError, AttributeError):
                self.broken = True
                done.set()
                break
            done.set()
        self.broken = True
        _close(self._stream)
        done.set()
        while True:
            try:
                _data, left, _result = self._queue.get_nowait()
            except queue.Empty:
                return
            left.set()

    def stop(self, timeout: float) -> None:
        """Make sure the thread ends: a stop message when it is idle (it
        is ignored by one already gone), then a bounded join."""
        if not self.stalled and not self.broken:
            self._queue.put((None, threading.Event(), [False]))
        self.thread.join(timeout)


class ClaudeStreamAdapter:
    """See the module docstring. ``resolved`` is the registry's
    :class:`~swe.remote.adapters.registry.Resolved` (None in tests that
    pass ``caps`` directly). ``spawn`` replaces ``subprocess.Popen``;
    ``clock`` the event timestamps; ``env`` the base environment
    (default ``os.environ``)."""

    adapter_id = "claude_stream"

    def __init__(self, resolved: Any = None, *, caps: Caps | None = None,
                 spawn: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 env: dict | None = None,
                 init_timeout: float = INIT_TIMEOUT_S,
                 call_timeout: float = CALL_TIMEOUT_S,
                 write_timeout: float = WRITE_TIMEOUT_S,
                 run_id: str = "") -> None:
        ceiling = caps if caps is not None else (
            getattr(resolved, "caps", None) or Caps())
        self._ceiling = ceiling.narrowed(wire.PROVEN)
        self._caps = self._ceiling
        self.resolved = resolved
        self._argv = getattr(resolved, "argv", None)
        self._min_version = getattr(resolved, "min_version", "") \
            or wire.MIN_VERSION
        self._spawn = spawn or subprocess.Popen
        self._clock = clock
        self._env = dict(os.environ if env is None else env)
        self._init_timeout = init_timeout
        self._call_timeout = call_timeout
        self._write_timeout = write_timeout
        self._run_id = run_id
        self._lock = threading.RLock()
        self._writer: _Writer | None = None
        self._child_env: dict = {}
        self._proc: Any = None
        self._sink: EventSink | None = None
        self._state: wire.StreamState | None = None
        self._spec: StartSpec | None = None
        self._session_id = ""
        self._cwd = ""
        self._closed = False
        self._failed = False
        self._waiters: dict[str, _Waiter] = {}
        self._n = 0
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._stderr: collections.deque = collections.deque(
            maxlen=_STDERR_LINES)
        self._mode_before_plan = ""
        # Set once the CLI's first init message has come (or it exited):
        # only that message says whether it takes steer.
        self._init_seen = threading.Event()
        self.version = ""
        # Why lines were skipped (a bug, never the wire's fault): kept
        # for tests and logs, bounded.
        self.dropped: collections.deque = collections.deque(maxlen=20)

    # -- small helpers ---------------------------------------------------

    @property
    def capabilities(self) -> Caps:
        """The caps in force: harness.json's met with what the fixtures
        prove, then with the handshake, then with each init message's
        ``capabilities`` array (``StreamState`` narrows those)."""
        state = self._state
        return state.caps if state is not None and state.started \
            else self._caps

    @property
    def session_id(self) -> str:
        return self._session_id

    def _rid(self, what: str) -> str:
        self._n += 1
        return f"swe-{self._n}-{what}"

    def _emit(self, events: list) -> None:
        sink = self._sink
        if sink is None:
            return
        for event in events:
            sink.emit(event)

    def _write(self, message: dict, *, wait: bool = True) -> bool:
        """Write one line through the writer thread: True once written
        (or queued, with ``wait`` False), False when the pipe is gone.
        A write that misses ``write_timeout`` kills the child (the CLI
        stopped reading; its exit sequence follows) and raises
        AdapterError ``timeout``."""
        writer = self._writer
        if writer is None:
            return False
        try:
            return writer.send(wire.encode(message),
                               self._write_timeout if wait else None)
        except _Stalled:
            self._kill()
            raise AdapterError("claude stopped reading its input",
                               code="timeout") from None

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except (OSError, AttributeError):
            pass

    def _live(self) -> wire.StreamState:
        if self._state is None or self._spec is None:
            raise AdapterError("the session is not started",
                               code="not_started")
        if self._state.done or self._closed:
            raise AdapterError("the agent is not running", code="agent_down")
        return self._state

    def _call(self, message: dict, timeout: float | None = None) -> dict:
        """Send one control request and wait for its answer; the answer's
        body. Raises AdapterError: ``agent_down`` (write failed or the
        CLI exited), ``timeout``, ``refused`` (an error answer)."""
        return self._await(message, self._request(message), timeout)

    def _request(self, message: dict) -> _Waiter:
        """Write one control request with a waiter for its answer."""
        rid = message["request_id"]
        waiter = _Waiter()
        with self._lock:
            self._waiters[rid] = waiter
            try:
                written = self._write(message)
            except AdapterError:
                self._waiters.pop(rid, None)
                raise
            if not written:
                self._waiters.pop(rid, None)
                raise AdapterError("claude is not reachable",
                                   code="agent_down")
        return waiter

    def _await(self, message: dict, waiter: _Waiter,
               timeout: float | None = None) -> dict:
        rid = message["request_id"]
        subtype = message["request"]["subtype"]
        if not waiter.event.wait(self._call_timeout if timeout is None
                                 else timeout):
            with self._lock:
                self._waiters.pop(rid, None)
            raise AdapterError(f"claude did not answer {subtype}",
                               code="timeout")
        ack = waiter.ack
        if ack is None:
            raise AdapterError(f"claude exited during {subtype}",
                               code="agent_down")
        if not ack.ok:
            raise AdapterError(f"claude refused {subtype}: "
                               f"{ack.error[:200]}", code="refused")
        return ack.body

    def _stderr_tail(self) -> str:
        return wire.redact(" | ".join(self._stderr))[-400:]

    # -- SessionAdapter --------------------------------------------------

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        if self._closed:
            raise AdapterError("the adapter is closed", code="closed")
        if self._spec is not None or self._failed:
            raise AdapterError("already started", code="started")
        if spec.fork:
            self._caps.require("fork")
        if spec.resume_id and not wire.is_session_id(spec.resume_id):
            raise AdapterError(f"{spec.resume_id!r} is not a Claude "
                               "session id", code="bad_request")
        session_id = spec.resume_id if spec.resume_id and not spec.fork \
            else wire.new_session_id()
        base = spec.argv or self._argv or wire.DEFAULT_ARGV
        mode = "plan" if spec.plan_mode else spec.permission_mode
        argv = wire.spawn_argv(
            base, session_id="" if session_id == spec.resume_id
            else session_id, resume_id=spec.resume_id, fork=spec.fork,
            model=spec.model, effort=spec.effort, permission_mode=mode)
        self._spec = spec
        self._sink = sink
        self._cwd = spec.cwd
        self._session_id = session_id
        self._state = wire.StreamState(spec.session_key, clock=self._clock,
                                       caps=self._caps,
                                       session_id=session_id,
                                       run_id=self._run_id)
        self._child_env = wire.spawn_env(self._env, dict(spec.env))
        try:
            self._proc = self._spawn(
                argv, cwd=spec.cwd, env=self._child_env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except (OSError, ValueError) as exc:
            self._failed = True
            raise AdapterError(f"claude did not start: {exc}",
                               code="agent_down") from None
        self._writer = _Writer(self._proc.stdin)
        self._err_reader = threading.Thread(target=self._read_stderr,
                                            daemon=True,
                                            name="claude-stream-stderr")
        self._err_reader.start()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="claude-stream")
        self._reader.start()
        try:
            body = self._call(wire.initialize(self._rid("initialize")),
                              self._init_timeout)
            hs = wire.Handshake.from_body(body)
            self.version = hs.version
            if not wire.version_ok(hs.version, self._min_version):
                raise AdapterError(
                    f"claude {hs.version or '(unknown version)'} is older "
                    f"than {self._min_version}, the oldest this adapter "
                    "drives; update Claude Code", code="unsupported_version")
        except AdapterError as exc:
            self._abort()
            if exc.code == "agent_down":
                tail = self._stderr_tail()
                raise AdapterError(
                    "claude exited before the handshake"
                    + (f": {tail}" if tail else ""),
                    code="agent_down") from None
            raise
        with self._lock:
            self._caps = self._caps.narrowed(hs.caps())
            state = self._state
            state.permission_mode = hs.permission_mode
            if state.done:
                self._failed = True
                raise AdapterError("claude exited during the handshake",
                                   code="agent_down")
            self._emit(state.start_events(self._caps, session_id))
        return session_id

    def _abort(self) -> None:
        """A start that failed: stop the child, emit nothing."""
        self._failed = True
        with self._lock:
            if self._state is not None:
                self._state.done = True
        self._stop_process(0.5)

    def send_turn(self, turn: TurnInput) -> str:
        with self._lock:
            state = self._live()
            if state.turn is not None:
                raise AdapterError("a turn is already running", code="busy")
            turn_id, events = state.begin_turn(
                turn.text, attachments=turn.attachments,
                command_id=turn.command_id, device=turn.device)
            try:
                written = self._write(wire.user_message(
                    turn.text, attachments=turn.attachments))
            except AdapterError:
                written = None
            if not written:
                # Never emitted: the turn did not start.
                state.turn = None
                if written is None:
                    raise AdapterError("claude stopped reading its input",
                                       code="timeout")
                raise AdapterError("claude is not reachable",
                                   code="agent_down")
            self._emit(events)
            return turn_id

    def _steerable(self) -> wire.StreamState:
        state = self._live()
        self.capabilities.require("steer")
        if state.turn is None:
            raise AdapterError("no turn to steer", code="idle")
        return state

    def steer(self, turn: TurnInput) -> None:
        """Send ``turn`` with ``priority: "now"``: the CLI stops the reply
        in progress and answers this message inside the same turn (see
        docs/specs/G7-claude-stream-adapter.md, "Steer"). Needs an open
        turn (``idle`` otherwise, as the fake adapter). The CLI says
        whether it takes this (``interrupt_send_now_v1``) only in the
        turn's init message, so a steer sent before this process's first
        one waits for it, bounded by ``call_timeout``."""
        with self._lock:
            ready = self._steerable().init_seen
        if not ready and not self._init_seen.wait(self._call_timeout):
            raise AdapterError("claude has not said whether it takes steer",
                               code="timeout")
        with self._lock:
            # Checked again: that init may have narrowed steer off, or
            # the turn may have ended meanwhile.
            state = self._steerable()
            try:
                written = self._write(wire.user_message(
                    turn.text, attachments=turn.attachments,
                    priority="now"))
            except AdapterError:
                written = None
            if not written:
                # Nothing emitted: the message never reached the CLI.
                if written is None:
                    raise AdapterError("claude stopped reading its input",
                                       code="timeout")
                raise AdapterError("claude is not reachable",
                                   code="agent_down")
            self._emit(state.steer(turn.text, attachments=turn.attachments,
                                   command_id=turn.command_id,
                                   device=turn.device))

    def interrupt(self) -> None:
        message = wire.interrupt(self._rid("interrupt"))
        with self._lock:
            state = self._live()
            if state.turn is None:
                return
            # Written first, under the lock: a failed write retires
            # nothing (the exit does), and the reader cannot slip the
            # CLI's answer in ahead of these events.
            waiter = self._request(message)
            self._emit(state.interrupt_sent())
        self._await(message, waiter)

    def respond(self, request_id: str, answer: Answer) -> None:
        with self._lock:
            state = self._live()
            pending = state.pending.get(request_id)
            reply, events = state.answer(request_id, answer)
            try:
                written = self._write(reply)
            except AdapterError:
                state.pending[request_id] = pending
                raise
            if not written:
                # Still open: the exit sequence retires it.
                state.pending[request_id] = pending
                raise AdapterError("claude is not reachable",
                                   code="agent_down")
            self._emit(events)

    def set_option(self, name: str, value: Any) -> None:
        with self._lock:
            state = self._live()
        check_option(self.capabilities, name, value)
        if name == "model":
            self._call(wire.set_model(self._rid("set_model"), value))
            return
        if name == "permission_mode":
            mode = value
        elif value:
            if state.permission_mode != "plan":
                self._mode_before_plan = state.permission_mode
            mode = "plan"
        else:
            mode = self._mode_before_plan or "default"
        body = self._call(wire.set_permission_mode(
            self._rid("set_permission_mode"), mode))
        with self._lock:
            state.permission_mode = body.get("mode") if isinstance(
                body.get("mode"), str) else mode

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            state = self._live()
        self.capabilities.require("stop_task")
        known = state.tasks.get(task_id)
        if known is None or known["status"] != "running":
            raise AdapterError(f"no running task {task_id!r}",
                               code="unknown_task")
        self._call(wire.stop_task(self._rid("stop_task"), task_id))

    def rollback(self, turn_id: str) -> None:
        with self._lock:
            self._live()
        # rewind_files restores files, not the conversation: no wire form.
        self.capabilities.require("rollback")
        raise Unsupported("rollback")  # pragma: no cover - PROVEN keeps it off

    def transcript_position(self) -> TranscriptPosition | None:
        """Where Claude Code writes this session's transcript:
        ``<config dir>/projects/<cwd slug>/<session id>.jsonl``, with the
        config dir from ``CLAUDE_CONFIG_DIR`` or ``~/.claude`` as the
        child saw them (the environment it was started with)."""
        if not self._session_id or not self._cwd:
            return None
        env = self._child_env or self._env
        root = env.get("CLAUDE_CONFIG_DIR") or os.path.join(
            env.get("HOME") or os.path.expanduser("~"), ".claude")
        path = Path(root, "projects",
                    wire.transcript_slug(os.path.realpath(self._cwd)),
                    f"{self._session_id}.jsonl")
        try:
            return TranscriptPosition(str(path), path.stat().st_size)
        except OSError:
            return None

    def close(self, grace_s: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._state is not None and self._sink is not None \
                    and not self._failed:
                self._emit(self._state.closed())
            elif self._state is not None:
                self._state.done = True
        self._stop_process(grace_s)

    # -- the process -----------------------------------------------------

    def _stop_process(self, grace_s: float) -> None:
        proc = self._proc
        if proc is None:
            return
        writer = self._writer
        eof = False
        if writer is not None and not writer.stalled:
            # EOF goes through the writer, behind anything queued; a
            # writer stuck on a full pipe gets no say, the kill ends it.
            try:
                eof = writer.send(None, min(max(grace_s, 0.1), 1.0))
            except _Stalled:
                eof = False
        try:
            proc.wait(timeout=max(0.0, grace_s) if eof else 0)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=_KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=_KILL_WAIT_S)
                except (OSError, subprocess.SubprocessError):
                    pass
            except OSError:
                pass
        if writer is not None:
            # Gone with the child: a write the kill broke has ended it.
            writer.stop(_KILL_WAIT_S)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=_KILL_WAIT_S)

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for raw in proc.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._stderr.append(text[:300])
        except (OSError, ValueError, AttributeError):
            pass
        _close(getattr(proc, "stderr", None))

    def _read_loop(self) -> None:
        proc = self._proc
        try:
            for raw in proc.stdout:
                msg = wire.decode(raw)
                if msg is None:
                    continue
                try:
                    self._dispatch(msg)
                except Exception as exc:  # noqa: BLE001 - one line skipped
                    # A line the state machine choked on must not end
                    # the reader: every later line would be lost.
                    self.dropped.append(f"{type(exc).__name__}: {exc}"[:200])
                    continue
        except (OSError, ValueError):
            pass
        _close(proc.stdout)
        self._on_eof()

    def _dispatch(self, msg: dict) -> None:
        with self._lock:
            state = self._state
            if state is None:
                return
            fed = state.feed(msg)
            for reply in fed.replies:
                # Queued, never waited on: the reader must keep reading
                # even when the CLI has stopped reading us.
                self._write(reply, wait=False)
            if not state.done and self._sink is not None:
                self._emit(fed.events)
            if state.init_seen:
                self._init_seen.set()
            waiters = [(self._waiters.pop(a.request_id, None), a)
                       for a in fed.acks]
        for waiter, ack in waiters:
            if waiter is not None:
                waiter.ack = ack
                waiter.event.set()

    def _on_eof(self) -> None:
        proc = self._proc
        try:
            code = proc.wait(timeout=_KILL_WAIT_S)
        except (subprocess.TimeoutExpired, OSError):
            code = None
        if self._err_reader is not None:
            # Its last words explain the exit; they reach the pipe first.
            self._err_reader.join(timeout=1.0)
        with self._lock:
            waiters = list(self._waiters.values())
            self._waiters.clear()
            state = self._state
            if state is not None and not state.started:
                # Gone before the handshake: start() reports it.
                state.done = True
            elif state is not None and not state.done and not self._closed \
                    and not self._failed:
                reason = "claude exited" + (
                    f" with code {code}" if code not in (None, 0) else "")
                tail = self._stderr_tail()
                self._emit(state.exited(reason + (f": {tail}" if tail
                                                  and code else "")))
        for waiter in waiters:
            waiter.event.set()
        # A steer waiting for the init wakes and finds the agent gone.
        self._init_seen.set()


def _close(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass


registry.register(ClaudeStreamAdapter.adapter_id, ClaudeStreamAdapter)
