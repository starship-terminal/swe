"""The Codex session adapter: one ``codex app-server`` process per session.

``CodexAppServerAdapter`` starts the endpoint (harness.json's
``capabilities.session.argv``, or ``codex app-server``), does the
JSON-RPC handshake (``initialize``, ``initialized``, then
``thread/start``, ``thread/resume`` or ``thread/fork``), and from then
on reads the server's stdout on its own thread, turning every line into
v2 events through :class:`codex_wire.ThreadState`. Everything about the
wire format is in ``codex_wire.py``; this module owns the process, the
threads, the locks and the bounded waits, under the contract in
``base.py``.

- ``start`` blocks on the handshake (``INIT_TIMEOUT_S`` per request),
  checks the CLI version against ``min_version``, and returns the id
  SWE's session list knows the thread by (its rollout file's stem). The
  server assigns thread ids, so ``StartSpec.session_id`` is not used.
- ``send_turn`` writes ``turn/start`` and returns once the server
  accepts it; the turn ends at ``turn/completed``.
- ``steer`` writes ``turn/steer`` into the open turn.
- ``interrupt`` writes ``turn/interrupt``.
- ``respond`` writes the JSON-RPC answer to an open approval.
- ``set_option`` keeps ``model``, ``effort`` and ``permission_mode``
  (Codex's ``approvalPolicy``) for the next ``turn/start``, which
  carries them as overrides the server then keeps.
- ``rollback`` writes ``thread/revert`` (conversation only).
- ``close`` retires what is open, ends the turn, emits ``closed``, then
  closes stdin (the server exits on EOF), and terminates and kills it
  if it lingers.
"""

from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
from typing import Any, Callable

from swe.remote.adapters import codex_wire as wire
from swe.remote.adapters import registry
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, EventSink, StartSpec, TranscriptPosition,
    TurnInput, Unsupported, check_option,
)
from swe.remote.adapters.core import (
    Ack, LineWriter, Stalled, close_quietly, redact,
)

# How long each handshake request, any later request, one stdin write
# and the kill steps get.
INIT_TIMEOUT_S = 60.0
# The model list asked at start feeds only the picker (G32): a slow one is
# given up on, and the start goes on without it.
MODEL_LIST_TIMEOUT_S = 5.0
CALL_TIMEOUT_S = 15.0
WRITE_TIMEOUT_S = 10.0
_KILL_WAIT_S = 3.0

# Lines of the server's stderr kept to explain a failed start or an exit.
_STDERR_LINES = 20


class _Waiter:
    __slots__ = ("event", "ack")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.ack: Ack | None = None


class CodexAppServerAdapter:
    """See the module docstring. ``resolved`` is the registry's
    :class:`~swe.remote.adapters.registry.Resolved` (None in tests that
    pass ``caps`` directly). ``spawn`` replaces ``subprocess.Popen``;
    ``clock`` the event timestamps; ``env`` the base environment
    (default ``os.environ``)."""

    adapter_id = "codex_appserver"

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
        self._caps = ceiling.narrowed(wire.PROVEN)
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
        self._writer: LineWriter | None = None
        self._proc: Any = None
        self._sink: EventSink | None = None
        self._state: wire.ThreadState | None = None
        self._spec: StartSpec | None = None
        self._session_id = ""
        self._rollout = ""
        self._closed = False
        self._failed = False
        self._waiters: dict[Any, _Waiter] = {}
        self._n = 0
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._stderr: collections.deque = collections.deque(
            maxlen=_STDERR_LINES)
        # Options changed since the last accepted turn/start; that
        # request carries them, and the server keeps them from then on.
        self._overrides: dict[str, str] = {}
        self.version = ""
        # Why lines were skipped (a bug, never the wire's fault): kept
        # for tests and logs, bounded.
        self.dropped: collections.deque = collections.deque(maxlen=20)

    # -- small helpers ---------------------------------------------------

    @property
    def capabilities(self) -> Caps:
        """The caps in force: harness.json's met with what the fixtures
        prove, then with what the server has refused since."""
        state = self._state
        return state.caps if state is not None and state.started \
            else self._caps

    @property
    def session_id(self) -> str:
        return self._session_id

    def _rpc_id(self) -> int:
        self._n += 1
        return self._n

    def _emit(self, events: list) -> None:
        sink = self._sink
        if sink is None:
            return
        for event in events:
            sink.emit(event)

    def _write(self, message: dict, *, wait: bool = True) -> bool:
        """Write one line through the writer thread: True once written
        (or queued, with ``wait`` False), False when the pipe is gone.
        A write that misses ``write_timeout`` kills the child (the
        server stopped reading; its exit sequence follows) and raises
        AdapterError ``timeout``."""
        writer = self._writer
        if writer is None:
            return False
        try:
            return writer.send(wire.encode(message),
                               self._write_timeout if wait else None)
        except Stalled:
            self._kill()
            raise AdapterError("codex stopped reading its input",
                               code="timeout") from None

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except (OSError, AttributeError):
            pass

    def _live(self) -> wire.ThreadState:
        if self._state is None or self._spec is None:
            raise AdapterError("the session is not started",
                               code="not_started")
        if self._state.done or self._closed:
            raise AdapterError("the agent is not running", code="agent_down")
        return self._state

    def _request(self, message: dict) -> _Waiter:
        """Write one request with a waiter for its answer."""
        rpc_id = message["id"]
        waiter = _Waiter()
        with self._lock:
            self._waiters[rpc_id] = waiter
            try:
                written = self._write(message)
            except AdapterError:
                self._waiters.pop(rpc_id, None)
                raise
            if not written:
                self._waiters.pop(rpc_id, None)
                raise AdapterError("codex is not reachable",
                                   code="agent_down")
        return waiter

    def _await(self, message: dict, waiter: _Waiter,
               timeout: float | None = None) -> Ack:
        """The answer to ``message``, error answers included. Raises
        AdapterError ``timeout`` or ``agent_down``."""
        method = message["method"]
        if not waiter.event.wait(self._call_timeout if timeout is None
                                 else timeout):
            with self._lock:
                self._waiters.pop(message["id"], None)
            raise AdapterError(f"codex did not answer {method}",
                               code="timeout")
        if waiter.ack is None:
            raise AdapterError(f"codex exited during {method}",
                               code="agent_down")
        return waiter.ack

    def _call(self, message: dict, timeout: float | None = None) -> dict:
        """Send one request and wait for its answer; the answer's body.
        Raises AdapterError: ``agent_down``, ``timeout``, ``refused``
        (an error answer)."""
        ack = self._await(message, self._request(message), timeout)
        if not ack.ok:
            raise _refused(message["method"], ack)
        return ack.body

    def _stderr_tail(self) -> str:
        return redact(" | ".join(self._stderr))[-400:]

    # -- SessionAdapter --------------------------------------------------

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        if self._closed:
            raise AdapterError("the adapter is closed", code="closed")
        if self._spec is not None or self._failed:
            raise AdapterError("already started", code="started")
        if spec.fork:
            self._caps.require("fork")
        thread_id = wire.thread_id_of(spec.resume_id)
        if spec.resume_id and not thread_id:
            raise AdapterError(f"{spec.resume_id!r} names no Codex thread",
                               code="bad_request")
        argv = list(spec.argv or self._argv or wire.DEFAULT_ARGV)
        self._spec = spec
        self._sink = sink
        self._state = wire.ThreadState(spec.session_key, clock=self._clock,
                                       caps=self._caps, run_id=self._run_id)
        if spec.effort:
            self._overrides["effort"] = spec.effort
        try:
            self._proc = self._spawn(
                argv, cwd=spec.cwd, env={**self._env, **dict(spec.env)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except (OSError, ValueError) as exc:
            self._failed = True
            raise AdapterError(f"codex did not start: {exc}",
                               code="agent_down") from None
        self._writer = LineWriter(self._proc.stdin, "codex-appserver-stdin")
        self._err_reader = threading.Thread(target=self._read_stderr,
                                            daemon=True,
                                            name="codex-appserver-stderr")
        self._err_reader.start()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="codex-appserver")
        self._reader.start()
        try:
            thread = self._handshake(spec, thread_id)
        except AdapterError as exc:
            self._abort()
            if exc.code == "agent_down":
                tail = self._stderr_tail()
                raise AdapterError(
                    "codex exited before the handshake"
                    + (f": {tail}" if tail else ""),
                    code="agent_down") from None
            raise
        with self._lock:
            state = self._state
            if state.done:
                self._failed = True
                raise AdapterError("codex exited during the handshake",
                                   code="agent_down")
            state.thread_id = str(thread.get("id") or "")
            self._session_id = wire.session_ref(thread)
            self._rollout = str(thread.get("path") or "")
            # An effort the launch asked for goes out with the first
            # turn (the thread's start takes none).
            for name, value in self._overrides.items():
                state.settings[name] = value
            self._emit(state.start_events(self._caps, self._session_id))
        return self._session_id

    def _handshake(self, spec: StartSpec, thread_id: str) -> dict:
        from swe import __version__

        hello = self._call(wire.initialize(self._rpc_id(), __version__),
                           self._init_timeout)
        self.version = wire.agent_version(hello.get("userAgent"))
        self._check_version()
        if not self._write(wire.initialized()):
            raise AdapterError("codex is not reachable", code="agent_down")
        fields = {"cwd": spec.cwd, "model": spec.model,
                  "approval_policy": spec.permission_mode}
        if spec.fork:
            message = wire.thread_fork(self._rpc_id(), thread_id, **fields)
        elif thread_id:
            message = wire.thread_resume(self._rpc_id(), thread_id, **fields)
        else:
            message = wire.thread_start(self._rpc_id(), **fields)
        answer = self._call(message, self._init_timeout)
        thread = answer.get("thread")
        if isinstance(self._state, wire.ThreadState):
            self._state.thread_settings(answer)
        if not isinstance(thread, dict) or not thread.get("id"):
            raise AdapterError("codex named no thread", code="refused")
        if not self.version:
            self.version = str(thread.get("cliVersion") or "")
            self._check_version()
        if self._caps.can_set("model") \
                and isinstance(self._state, wire.ThreadState):
            # The server's own list for the picker (G32); a server that
            # refuses it leaves the value alone.
            try:
                listed = self._call(wire.model_list(self._rpc_id()), min(
                    self._init_timeout, MODEL_LIST_TIMEOUT_S))
            except AdapterError as exc:
                if exc.code == "agent_down":
                    raise
                listed = {}
            self._state.model_list(listed)
        return thread

    def _check_version(self) -> None:
        if self.version and not wire.version_ok(self.version,
                                                self._min_version):
            raise AdapterError(
                f"codex {self.version} is older than {self._min_version}, "
                "the oldest this adapter drives; update Codex",
                code="unsupported_version")

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
            if state.busy:
                raise AdapterError("a turn is already running", code="busy")
            rpc_id = self._rpc_id()
            overrides = dict(self._overrides)
            message = wire.turn_start(
                rpc_id, state.thread_id, turn.text,
                attachments=turn.attachments,
                model=overrides.get("model", ""),
                effort=overrides.get("effort", ""),
                approval_policy=overrides.get("permission_mode", ""))
            state.expect_turn(rpc_id, turn.text, attachments=turn.attachments,
                              command_id=turn.command_id, device=turn.device)
            try:
                waiter = self._request(message)
            except AdapterError:
                state.forget(rpc_id)
                raise
        ack = self._settle(message, waiter)
        with self._lock:
            turn_id = state.accepted.pop(rpc_id, "")
            if turn_id:
                # Sent: the server keeps these from this turn on.
                for name, value in overrides.items():
                    if self._overrides.get(name) == value:
                        del self._overrides[name]
                return turn_id
        raise _refused("turn/start", ack)

    def _settle(self, message: dict, waiter: _Waiter) -> Ack | None:
        """Wait for the answer to a ``turn/start`` or ``turn/steer``.
        The state machine opens the turn (or adds the message) itself
        when the answer is read, so an answer that came in just past the
        deadline still counts: only one never read is a timeout."""
        rpc_id = message["id"]
        try:
            return self._await(message, waiter)
        except AdapterError:
            with self._lock:
                state = self._state
                if state is not None and rpc_id in state.accepted:
                    return None
                if state is not None:
                    state.forget(rpc_id)
            raise

    def steer(self, turn: TurnInput) -> None:
        """Add ``turn`` to the turn in flight with ``turn/steer``. The
        server stops the reply it is writing and answers the message in
        the same turn (docs/specs/G16-more-adapters.md, "Codex steer").
        Needs an open turn (``idle`` otherwise)."""
        with self._lock:
            state = self._live()
            self.capabilities.require("steer")
            if state.turn is None or not state.turn.native_id:
                raise AdapterError("no turn to steer", code="idle")
            rpc_id = self._rpc_id()
            message = wire.turn_steer(rpc_id, state.thread_id,
                                      state.turn.native_id, turn.text,
                                      attachments=turn.attachments)
            state.expect_steer(rpc_id, turn.text,
                               attachments=turn.attachments,
                               command_id=turn.command_id,
                               device=turn.device)
            try:
                waiter = self._request(message)
            except AdapterError:
                state.forget(rpc_id)
                raise
        ack = self._settle(message, waiter)
        with self._lock:
            if state.accepted.pop(rpc_id, ""):
                return
            if ack is not None and ack.code == wire.METHOD_NOT_FOUND:
                # This server has no turn/steer: say so, once.
                events: list = []
                state.narrow(events, Caps(
                    **{f: f != "steer" for f in ("steer", "plan_mode",
                                                 "rollback", "fork",
                                                 "stop_task")},
                    options=state.caps.options))
                self._emit(events)
                raise Unsupported("steer")
            if state.turn is None or (
                    ack is not None and "no active turn" in ack.error):
                raise AdapterError("no turn to steer", code="idle")
        raise _refused("turn/steer", ack)

    def interrupt(self) -> None:
        with self._lock:
            state = self._live()
            turn = state.turn
            if turn is None or not turn.native_id:
                return
            message = wire.turn_interrupt(self._rpc_id(), state.thread_id,
                                          turn.native_id)
            # Written first, under the lock: a failed write retires
            # nothing (the exit does), and the reader cannot slip the
            # server's answer in ahead of these events.
            waiter = self._request(message)
            self._emit(state.interrupt_sent())
        ack = self._await(message, waiter)
        if not ack.ok and "no active turn" not in ack.error:
            raise _refused("turn/interrupt", ack)

    def respond(self, request_id: str, answer: Answer) -> None:
        with self._lock:
            state = self._live()
            pending = state.pending.get(request_id)
            reply, events = state.answer(request_id, answer)
            try:
                written = self._write(reply)
            except AdapterError:
                state.reopen(request_id, pending)
                raise
            if not written:
                # Still open: the exit sequence retires it.
                state.reopen(request_id, pending)
                raise AdapterError("codex is not reachable",
                                   code="agent_down")
            self._emit(events)

    def set_option(self, name: str, value: Any) -> None:
        with self._lock:
            self._live()
            check_option(self.capabilities, name, value)
            if name == "permission_mode" \
                    and value not in wire.APPROVAL_POLICIES:
                raise AdapterError(
                    f"{value!r} is not a Codex approval policy "
                    f"({', '.join(wire.APPROVAL_POLICIES)})",
                    code="bad_request")
            # plan_mode never gets here: PROVEN keeps it off.
            self._overrides[name] = value
            self._emit(self._state.setting(name, value))

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            self._live()
        self.capabilities.require("stop_task")
        raise Unsupported("stop_task")  # pragma: no cover - PROVEN keeps it off

    def rollback(self, turn_id: str) -> None:
        """Drop ``turn_id`` and every later turn from the thread with
        ``thread/revert``. Only a turn this adapter started can be
        named: the server's own id for an older one is not known. The
        server writes the shortened history to a new rollout file, so
        the session's id (that file's stem) changes: a ``session.state``
        says the new one."""
        with self._lock:
            state = self._live()
            self.capabilities.require("rollback")
            if state.busy:
                raise AdapterError("a turn is running", code="busy")
            native = state.native_turns.get(turn_id)
            if not native:
                raise AdapterError(f"no turn {turn_id!r} from this start",
                                   code="unknown_turn")
            message = wire.thread_revert(self._rpc_id(), state.thread_id,
                                         native)
        thread = self._call(message).get("thread")
        with self._lock:
            ids = list(state.native_turns)
            for later in ids[ids.index(turn_id):] if turn_id in ids else ():
                state.native_turns.pop(later, None)
            # The reverted history is a new rollout file: follow it.
            moved = state.moved(thread)
            if moved:
                self._session_id = state.session_id
                self._rollout = str(thread.get("path") or "")
                self._emit(moved)

    def transcript_position(self) -> TranscriptPosition | None:
        """The thread's rollout file, as the server named it."""
        if not self._rollout:
            return None
        try:
            return TranscriptPosition(self._rollout,
                                      os.stat(self._rollout).st_size)
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
            except Stalled:
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
        close_quietly(getattr(proc, "stderr", None))

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
        close_quietly(proc.stdout)
        self._on_eof()

    def _dispatch(self, msg: dict) -> None:
        with self._lock:
            state = self._state
            if state is None:
                return
            fed = state.feed(msg)
            for reply in fed.replies:
                # Queued, never waited on: the reader must keep reading
                # even when the server has stopped reading us.
                self._write(reply, wait=False)
            if not state.done and self._sink is not None:
                self._emit(fed.events)
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
                reason = "codex exited" + (
                    f" with code {code}" if code not in (None, 0) else "")
                tail = self._stderr_tail()
                self._emit(state.exited(reason + (f": {tail}" if tail
                                                  and code else "")))
        for waiter in waiters:
            waiter.event.set()


def _refused(method: str, ack: Ack | None) -> AdapterError:
    detail = redact(ack.error)[:200] if ack is not None else "no answer"
    return AdapterError(f"codex refused {method}: {detail}", code="refused")


registry.register(CodexAppServerAdapter.adapter_id, CodexAppServerAdapter)
