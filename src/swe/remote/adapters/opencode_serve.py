"""The opencode session adapter: one ``opencode serve`` process per session.

``OpencodeServeAdapter`` starts the server (harness.json's
``capabilities.session.argv``, or ``opencode serve``) bound to loopback
on a port the server picks, with a password only this process knows,
then talks to its HTTP API: one long ``GET /api/event`` stream read on
its own thread, and one short request per call. Every event goes
through :class:`opencode_wire.ServeState`, which turns it into v2
events. Everything about the wire format is in ``opencode_wire.py``;
this module owns the process, the connections, the threads, the locks
and the bounded waits, under the contract in ``base.py``.

- ``start`` waits for the server's "listening" line, checks its version
  against ``min_version``, opens the event stream, then creates the
  session (or fetches the one to resume, or forks it) and returns the
  server's id for it (``ses_...``). The server names sessions, so
  ``StartSpec.session_id`` is used only when it already is such an id.
- ``send_turn`` posts the prompt and returns once the server takes it;
  the turn ends at ``session.execution.succeeded``, ``.failed`` or
  ``.interrupted``.
- ``steer`` posts a prompt with ``delivery: steer`` into the open turn.
- ``interrupt`` posts ``/interrupt``.
- ``respond`` posts the reply to an open permission or form.
- ``set_option`` posts ``/model`` (``model``, ``effort``) or ``/agent``
  (``plan_mode``: the ``plan`` agent, and back).
- ``rollback`` stages and commits a revert to before a turn's message
  (conversation only).
- ``close`` retires what is open, ends the turn, emits ``closed``, then
  terminates the server, and kills it if it lingers.

Nothing waits without a bound: each request has ``call_timeout``, the
handshake ``init_timeout`` per step, and the stop ``grace_s`` plus the
kill wait. The password is in the child's environment and in this
object, never in an event, an error or a log line: opencode passes its
environment on to the commands its agent runs, so the password is
shaped for the redactor to mask (``opencode_wire.new_password``).
"""

from __future__ import annotations

import collections
import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable

from swe.remote.adapters import opencode_wire as wire
from swe.remote.adapters import registry
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, EventSink, StartSpec, TranscriptPosition,
    TurnInput, Unsupported, check_option,
)
from swe.remote.adapters.core import close_quietly, redact

# How long each handshake step, any later request and the kill steps get.
INIT_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 15.0
_KILL_WAIT_S = 3.0
# How long a server whose event stream ended gets to exit by itself.
_STREAM_GRACE_S = 1.0

# Lines of the server's output kept to explain a failed start or an exit.
_OUTPUT_LINES = 20
# The most one answer and one line of the event stream may hold.
_MAX_BODY = 16 * 1024 * 1024
_MAX_LINE = 16 * 1024 * 1024

_OK = (200, 204)


class _Reply:
    """One HTTP answer: the status and the decoded JSON body."""

    __slots__ = ("status", "body")

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return self.status in _OK

    @property
    def data(self) -> dict:
        data = self.body.get("data") if isinstance(self.body, dict) else None
        return data if isinstance(data, dict) else {}


class OpencodeServeAdapter:
    """See the module docstring. ``resolved`` is the registry's
    :class:`~swe.remote.adapters.registry.Resolved` (None in tests that
    pass ``caps`` directly). ``spawn`` replaces ``subprocess.Popen``;
    ``clock`` the event timestamps; ``env`` the base environment
    (default ``os.environ``)."""

    adapter_id = "opencode_serve"

    def __init__(self, resolved: Any = None, *, caps: Caps | None = None,
                 spawn: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 env: dict | None = None,
                 init_timeout: float = INIT_TIMEOUT_S,
                 call_timeout: float = CALL_TIMEOUT_S,
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
        self._run_id = run_id
        self._lock = threading.RLock()
        self._proc: Any = None
        self._sink: EventSink | None = None
        self._state: wire.ServeState | None = None
        self._spec: StartSpec | None = None
        self._session_id = ""
        self._closed = False
        self._failed = False
        self._password = ""
        self._host = ""
        self._port = 0
        self._listening = threading.Event()
        self._connected = threading.Event()
        self._stream: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._out_reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None
        self._output: collections.deque = collections.deque(
            maxlen=_OUTPUT_LINES)
        self._n = 0
        # What ``set_option`` last chose, to build the next model ref.
        self._model = ""
        self._effort = ""
        self._agent_before = ""
        self._exit_reason = ""
        self.version = ""
        # Why events were skipped (a bug, never the wire's fault): kept
        # for tests and logs, bounded.
        self.dropped: collections.deque = collections.deque(maxlen=20)

    # -- small helpers ---------------------------------------------------

    @property
    def capabilities(self) -> Caps:
        """The caps in force: harness.json's met with what the fixtures
        prove."""
        state = self._state
        return state.caps if state is not None and state.started \
            else self._caps

    @property
    def session_id(self) -> str:
        return self._session_id

    def _token(self) -> int:
        self._n += 1
        return self._n

    def _emit(self, events: list) -> None:
        sink = self._sink
        if sink is None:
            return
        for event in events:
            sink.emit(event)

    def _signal(self, hard: bool = False) -> None:
        """SIGTERM (or SIGKILL) to the server and to anything it
        started: it runs in its own process group, since an npm-style
        launcher keeps the real server as its child."""
        proc = self._proc
        try:
            # The group, whether or not its leader still runs: a
            # launcher can exit and leave the server it started.
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            return
        except (OSError, AttributeError):
            pass  # no such group (or no groups here): the child alone
        try:
            (proc.kill if hard else proc.terminate)()
        except (OSError, AttributeError):
            pass

    def _kill(self) -> None:
        self._signal(hard=True)

    def _live(self) -> wire.ServeState:
        if self._state is None or self._spec is None:
            raise AdapterError("the session is not started",
                               code="not_started")
        if self._state.done or self._closed:
            raise AdapterError("the agent is not running", code="agent_down")
        return self._state

    def _headers(self, accept: str = "application/json") -> dict:
        return {"Authorization": wire.basic_auth(self._password),
                "Accept": accept}

    def _call(self, request: wire.Request,
              timeout: float | None = None) -> _Reply:
        """One request and its answer, whatever the status. Raises
        AdapterError ``timeout`` (no answer in time) or ``agent_down``
        (no connection)."""
        timeout = self._call_timeout if timeout is None else timeout
        what = f"{request.method} {request.path.rsplit('/', 1)[-1]}"
        conn = http.client.HTTPConnection(self._host, self._port,
                                          timeout=timeout)
        try:
            headers = self._headers()
            data = None
            if request.body is not None:
                data = json.dumps(request.body,
                                  ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(request.method, request.path, body=data,
                         headers=headers)
            resp = conn.getresponse()
            raw = resp.read(_MAX_BODY + 1)
            status = resp.status
        except TimeoutError:
            raise AdapterError(f"opencode did not answer {what}",
                               code="timeout") from None
        except (OSError, http.client.HTTPException):
            raise AdapterError("opencode is not reachable",
                               code="agent_down") from None
        finally:
            conn.close()
        body: Any = None
        if raw and len(raw) <= _MAX_BODY:
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        return _Reply(status, body)

    def _ok(self, request: wire.Request, what: str,
            timeout: float | None = None) -> _Reply:
        """``_call``, with any answer but 200 and 204 an AdapterError
        ``refused``."""
        reply = self._call(request, timeout)
        if not reply.ok:
            raise _refused(what, reply)
        return reply

    def _output_tail(self) -> str:
        return redact(" | ".join(self._output))[-400:]

    # -- SessionAdapter --------------------------------------------------

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        if self._closed:
            raise AdapterError("the adapter is closed", code="closed")
        if self._spec is not None or self._failed:
            raise AdapterError("already started", code="started")
        if spec.fork:
            self._caps.require("fork")
        if spec.resume_id and not wire.is_session_id(spec.resume_id):
            raise AdapterError(
                f"{spec.resume_id!r} names no opencode session",
                code="bad_request")
        if spec.model and wire.model_ref(spec.model) is None:
            raise AdapterError(
                f"{spec.model!r} is not an opencode model (provider/model)",
                code="bad_request")
        argv = list(spec.argv or self._argv or wire.DEFAULT_ARGV)
        if "--port" not in argv:
            argv += wire.BIND_ARGS
        self._spec = spec
        self._sink = sink
        self._state = wire.ServeState(spec.session_key, clock=self._clock,
                                      caps=self._caps, run_id=self._run_id)
        self._password = wire.new_password()
        try:
            self._proc = self._spawn(
                argv, cwd=spec.cwd,
                env={**self._env, **dict(spec.env),
                     wire.PASSWORD_ENV: self._password},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as exc:
            self._failed = True
            raise AdapterError(f"opencode did not start: {exc}",
                               code="agent_down") from None
        self._err_reader = threading.Thread(
            target=self._read_output, args=(self._proc.stderr, False),
            daemon=True, name="opencode-serve-stderr")
        self._err_reader.start()
        self._out_reader = threading.Thread(
            target=self._read_output, args=(self._proc.stdout, True),
            daemon=True, name="opencode-serve-stdout")
        self._out_reader.start()
        try:
            self._handshake(spec)
        except AdapterError as exc:
            self._abort()
            if exc.code == "agent_down":
                tail = self._output_tail()
                raise AdapterError(
                    "opencode exited before the handshake"
                    + (f": {tail}" if tail else ""),
                    code="agent_down") from None
            raise
        return self._session_id

    def _handshake(self, spec: StartSpec) -> None:
        if not self._listening.wait(self._init_timeout):
            raise AdapterError("opencode never said it was listening",
                               code="timeout")
        if not self._port:
            raise AdapterError("opencode exited", code="agent_down")
        info = self._ok(wire.info(), "info", self._init_timeout)
        body = info.body if isinstance(info.body, dict) else {}
        self.version = str(body.get("version") or "")
        if self.version and not wire.version_ok(self.version,
                                                self._min_version):
            raise AdapterError(
                f"opencode {self.version} is older than {self._min_version}, "
                "the oldest this adapter drives; update opencode",
                code="unsupported_version")
        self._open_stream()
        if spec.fork:
            request = wire.fork_session(spec.resume_id)
        elif spec.resume_id:
            request = wire.get_session(spec.resume_id)
        else:
            request = wire.create_session(
                spec.cwd, model=spec.model, effort=spec.effort,
                session_id=spec.session_id)
        with self._lock:
            # Under the lock: the stream's reader takes no event of this
            # session before the state knows the session's id.
            state = self._state
            reply = self._call(request, self._init_timeout)
            if reply.status == 404:
                raise AdapterError(
                    f"opencode has no session {spec.resume_id!r}",
                    code="bad_request")
            if not reply.ok or not wire.is_session_id(reply.data.get("id")):
                raise _refused("the session", reply)
            if state.done:
                raise AdapterError("opencode exited", code="agent_down")
            session = reply.data
            self._session_id = session["id"]
            if isinstance(session.get("model"), dict):
                state.model = dict(session["model"])
            self._model = wire.model_name(state.model)
            self._effort = str(state.model.get("variant") or "")
            state.agent = str(session.get("agent") or "")
            if spec.resume_id:
                wanted = bool(spec.model or spec.effort)
            else:
                wanted = bool(spec.effort) and not spec.model
            if wanted:
                # A resumed or forked session keeps its model, and a new
                # one made with no model named took no effort: say them
                # now, over the model the server reports.
                model = spec.model or self._model
                effort = spec.effort or self._effort
                ref = wire.model_ref(model, effort)
                if ref is not None:
                    self._ok(wire.select_model(self._session_id, ref),
                             "the model", self._init_timeout)
                    self._model, self._effort = model, effort
                    state.model = dict(ref)
            if spec.plan_mode and self._caps.plan_mode:
                self._plan(True, self._init_timeout)
            if state.done:
                raise AdapterError("opencode exited", code="agent_down")
            # Last: a start that fails above has emitted nothing.
            self._emit(state.start_events(self._caps, self._session_id))
        if self._caps.can_set("model"):
            # The server's own list for the picker (G32), asked after the
            # start so it never holds the start up; it arrives as one more
            # session.options. A refused or late one leaves the value
            # alone (the keeper keeps the list it last had).
            threading.Thread(target=self._fetch_models, daemon=True,
                             name="opencode-serve-models").start()

    def _fetch_models(self) -> None:
        try:
            listed = self._call(wire.list_models())
        except AdapterError:
            return
        if not listed.ok:
            return
        with self._lock:
            state = self._state
            if state is not None and not state.done and not self._closed:
                state.model_choices = wire.model_choices(listed.body)
                self._emit(state.options_events())

    def _open_stream(self) -> None:
        """Connect ``GET /api/event`` and start its reader; returns once
        the server's first event (``server.connected``) is in."""
        # Our own socket, so the deadline can come off once the stream
        # is open (the connection object lets go of its socket when the
        # answer has no length).
        try:
            sock = socket.create_connection((self._host, self._port),
                                            timeout=self._init_timeout)
        except TimeoutError:
            raise AdapterError("opencode did not open its event stream",
                               code="timeout") from None
        except OSError:
            raise AdapterError("opencode is not reachable",
                               code="agent_down") from None
        conn = http.client.HTTPConnection(self._host, self._port)
        conn.sock = sock
        try:
            conn.request("GET", wire.events_stream().path,
                         headers=self._headers("text/event-stream"))
            resp = conn.getresponse()
        except TimeoutError:
            close_quietly(sock)
            raise AdapterError("opencode did not open its event stream",
                               code="timeout") from None
        except (OSError, http.client.HTTPException):
            close_quietly(sock)
            raise AdapterError("opencode is not reachable",
                               code="agent_down") from None
        if resp.status != 200:
            close_quietly(sock)
            raise AdapterError(
                f"opencode refused the event stream ({resp.status})",
                code="refused")
        # The stream is quiet between turns: no read deadline from here.
        # The server's exit ends it.
        sock.settimeout(None)
        self._stream = sock
        self._reader = threading.Thread(target=self._read_stream,
                                        args=(resp,), daemon=True,
                                        name="opencode-serve-events")
        self._reader.start()
        if not self._connected.wait(self._init_timeout):
            raise AdapterError("opencode's event stream stayed silent",
                               code="timeout")
        if self._state is not None and self._state.done:
            raise AdapterError("opencode exited", code="agent_down")

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
            token = self._token()
            state.expect_turn(token, turn.text, attachments=turn.attachments,
                              command_id=turn.command_id, device=turn.device)
        reply = self._post_prompt(state, token, wire.prompt(
            self._session_id, turn.text, attachments=turn.attachments))
        with self._lock:
            turn_id = state.accepted_turns.pop(token, "")
        if turn_id:
            return turn_id
        raise _not_taken("the prompt", reply)

    def _post_prompt(self, state: wire.ServeState, token: int,
                     request: wire.Request) -> _Reply | None:
        """Post a prompt and tell the state machine how it went. The
        turn's own events can overtake the answer and open the turn
        first, so an answer that never came still counts once they
        have: only a prompt nothing answered is an error."""
        try:
            reply = self._call(request)
        except AdapterError:
            with self._lock:
                if token in state.accepted_turns:
                    return None
                state.forget(token)
            raise
        with self._lock:
            if reply.ok and not state.done:
                self._emit(state.accepted(token, reply.data))
            else:
                state.forget(token)
        return reply

    def steer(self, turn: TurnInput) -> None:
        """Add ``turn`` to the turn in flight (``delivery: steer``). The
        server delivers it when the step it is on ends, and answers it
        in the same turn; one the turn never reaches is answered in a
        turn of its own (docs/specs/G16-more-adapters.md, "opencode
        steer"). Its user message is emitted when it is delivered.
        Needs an open turn (``idle`` otherwise)."""
        with self._lock:
            state = self._live()
            self.capabilities.require("steer")
            if state.turn is None:
                raise AdapterError("no turn to steer", code="idle")
            token = self._token()
            state.expect_steer(token, turn.text,
                               attachments=turn.attachments,
                               command_id=turn.command_id,
                               device=turn.device)
        reply = self._post_prompt(state, token, wire.prompt(
            self._session_id, turn.text, attachments=turn.attachments,
            steer=True))
        with self._lock:
            if state.accepted_turns.pop(token, None) is not None:
                return
        raise _not_taken("the steer", reply)

    def interrupt(self) -> None:
        with self._lock:
            state = self._live()
            if state.turn is None:
                return
            # Posted under the lock: a refused interrupt retires
            # nothing, and the reader cannot slip the turn's end in
            # ahead of these events.
            reply = self._call(wire.interrupt(self._session_id))
            if not reply.ok:
                raise _refused("the interrupt", reply)
            body = reply.body if isinstance(reply.body, dict) else {}
            if body.get("interrupted") is False:
                # Nothing was running there (the turn's end is on its
                # way): nothing was stopped, so nothing is retired.
                return
            self._emit(state.interrupt_sent())

    def respond(self, request_id: str, answer: Answer) -> None:
        with self._lock:
            state = self._live()
            pending = state.pending.get(request_id)
            request, events = state.answer(self._session_id, request_id,
                                           answer)
            # Posted under the lock, so ``approval.resolved`` goes out
            # before whatever the answer lets the server do next.
            try:
                reply = self._call(request)
            except AdapterError:
                # Still open: an exit retires it.
                state.reopen(request_id, pending)
                raise
            if not reply.ok:
                state.reopen(request_id, pending)
                raise _refused("the answer", reply)
            self._emit(events)

    def set_option(self, name: str, value: Any) -> None:
        with self._lock:
            self._live()
            check_option(self.capabilities, name, value)
            if name == "plan_mode":
                self._plan(value)
                return
            model = value if name == "model" else self._model
            effort = value if name == "effort" else self._effort
            ref = wire.model_ref(model, effort)
            if ref is None:
                raise AdapterError(
                    f"{model!r} is not an opencode model (provider/model)"
                    if model else "no model is selected yet: set one first",
                    code="bad_request")
            self._ok(wire.select_model(self._session_id, ref), "the model")
            self._model, self._effort = model, effort
            self._emit(self._state.model_selected(ref))

    def _plan(self, on: bool, timeout: float | None = None) -> None:
        """Switch to opencode's ``plan`` agent, or back to the agent the
        session ran before."""
        state = self._state
        current = state.agent if state is not None else ""
        if on:
            if current != wire.PLAN_AGENT:
                self._agent_before = current
            agent = wire.PLAN_AGENT
        else:
            agent = self._agent_before or wire.BUILD_AGENT
        self._ok(wire.select_agent(self._session_id, agent), "the agent",
                 timeout)
        if state is not None:
            self._emit(state.agent_selected(agent))

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            self._live()
        self.capabilities.require("stop_task")
        raise Unsupported("stop_task")  # pragma: no cover - PROVEN keeps it off

    def rollback(self, turn_id: str) -> None:
        """Drop ``turn_id`` and every later turn from the conversation:
        a staged revert to before the turn's first message, committed.
        Only a turn this adapter started can be named. Files are not
        touched."""
        with self._lock:
            state = self._live()
            self.capabilities.require("rollback")
            if state.busy:
                raise AdapterError("a turn is running", code="busy")
            message_id = state.turn_messages.get(turn_id)
            if not message_id:
                raise AdapterError(f"no turn {turn_id!r} from this start",
                                   code="unknown_turn")
            self._ok(wire.revert_stage(self._session_id, message_id),
                     "the revert")
            try:
                self._ok(wire.revert_commit(self._session_id), "the revert")
            except AdapterError:
                # Not half done: the next turn must not meet a staged
                # revert nobody committed.
                self._post_quietly(wire.revert_clear(self._session_id))
                raise
            ids = list(state.turn_messages)
            for later in ids[ids.index(turn_id):]:
                state.turn_messages.pop(later, None)

    def transcript_position(self) -> TranscriptPosition | None:
        """None: opencode keeps sessions in its own database, with no
        file to tail."""
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
        try:
            # The server has no "quit" request: SIGTERM is its way.
            self._signal()
            proc.wait(timeout=max(0.0, grace_s))
        except subprocess.TimeoutExpired:
            try:
                self._signal(hard=True)
                proc.wait(timeout=_KILL_WAIT_S)
            except (OSError, subprocess.SubprocessError):
                pass
        except OSError:
            pass
        # The child we started is gone; anything it left in its group
        # had the same SIGTERM and the same grace.
        self._signal(hard=True)
        stream = self._stream
        if stream is not None:
            # Unblocks a reader the server's exit did not.
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            close_quietly(stream)
        for thread in (self._reader, self._out_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=_KILL_WAIT_S)

    def _read_output(self, pipe: Any, is_stdout: bool) -> None:
        """Drain one of the server's pipes: the first "listening" line
        names the port; the last lines explain an exit. Stdout's end is
        the server's end."""
        try:
            for raw in pipe:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if not self._port:
                    url = wire.listening_url(text)
                    parts = urllib.parse.urlsplit(url) if url else None
                    if parts is not None and parts.hostname and parts.port:
                        self._host, self._port = parts.hostname, parts.port
                        self._listening.set()
                        continue
                self._output.append(text[:300])
        except (OSError, ValueError, AttributeError):
            pass
        close_quietly(pipe)
        if is_stdout:
            self._on_exit()

    def _read_stream(self, resp: Any) -> None:
        parser = wire.SseParser()
        oversized = False
        try:
            while True:
                raw = resp.readline(_MAX_LINE)
                if not raw:
                    break
                whole = raw.endswith(b"\n")
                if oversized or not whole:
                    # A line past the bound: drop the event it is in.
                    oversized = not whole
                    parser = wire.SseParser()
                    self.dropped.append("an event line over the size bound")
                    self._events_lost()
                    continue
                event = parser.feed(raw)
                if event is None:
                    continue
                if not self._connected.is_set():
                    self._connected.set()
                try:
                    self._dispatch(event)
                except Exception as exc:  # noqa: BLE001 - one event skipped
                    # An event the state machine choked on must not end
                    # the reader: every later event would be lost.
                    self.dropped.append(f"{type(exc).__name__}: {exc}"[:200])
                    self._events_lost()
        except (OSError, ValueError, http.client.HTTPException,
                AttributeError):
            pass
        self._connected.set()
        with self._lock:
            lost = not self._closed and not self._failed \
                and self._state is not None and not self._state.done
        if not lost:
            return
        try:
            # A server that is exiting closes the stream first: let its
            # own exit code tell the story. (Said from here too: the
            # stdout it wrote to may outlive it in a child's hands.)
            self._proc.wait(timeout=_STREAM_GRACE_S)
            self._on_exit()
        except subprocess.TimeoutExpired:
            # Alive with no events is no session: end it, and say why.
            with self._lock:
                self._exit_reason = "opencode closed its event stream"
            self._kill()
        except (OSError, AttributeError):
            pass

    def _events_lost(self) -> None:
        """The reader dropped an event, which may have been a post's
        echo (G32): forget the posts waiting for one, and read the
        session's model and agent back once, off the reader's thread.
        The stream itself never reconnects: when it ends, so does the
        session."""
        with self._lock:
            state = self._state
            if state is None or state.done or self._closed \
                    or not self._session_id:
                return
            generation = state.events_lost()
        threading.Thread(target=self._reread_session, args=(generation,),
                         daemon=True, name="opencode-serve-reread").start()

    def _reread_session(self, generation: int) -> None:
        try:
            reply = self._call(wire.get_session(self._session_id))
        except AdapterError:
            return
        if not reply.ok:
            return
        with self._lock:
            state = self._state
            if state is not None and not state.done and not self._closed:
                self._emit(state.session_read(reply.data, generation))

    def _dispatch(self, event: dict) -> None:
        with self._lock:
            state = self._state
            if state is None or not state.started:
                return
            fed = state.feed(event)
            if not state.done and self._sink is not None:
                self._emit(fed.events)
            replies = list(fed.replies)
        for request in replies:
            # Never waited on here: the reader must keep reading.
            threading.Thread(target=self._post_quietly, args=(request,),
                             daemon=True, name="opencode-serve-reply").start()

    def _post_quietly(self, request: wire.Request) -> None:
        try:
            self._call(request)
        except AdapterError:
            pass

    def _on_exit(self) -> None:
        proc = self._proc
        try:
            code = proc.wait(timeout=_KILL_WAIT_S)
        except (subprocess.TimeoutExpired, OSError):
            code = None
        self._listening.set()
        self._connected.set()
        if self._err_reader is not None:
            # Its last words explain the exit; they reach the pipe first.
            self._err_reader.join(timeout=1.0)
        with self._lock:
            state = self._state
            if state is not None and not state.started:
                # Gone before the handshake: start() reports it.
                state.done = True
            elif state is not None and not state.done and not self._closed \
                    and not self._failed:
                reason = self._exit_reason or ("opencode exited" + (
                    f" with code {code}" if code not in (None, 0) else ""))
                tail = self._output_tail()
                self._emit(state.exited(
                    reason + (f": {tail}" if tail and code
                              and not self._exit_reason else "")))


def _refused(what: str, reply: _Reply | None) -> AdapterError:
    detail = "no answer"
    if reply is not None:
        detail = redact(wire.error_text(reply.body))[:200] \
            or f"status {reply.status}"
    return AdapterError(f"opencode refused {what}: {detail}", code="refused")


def _not_taken(what: str, reply: _Reply | None) -> AdapterError:
    """Why a prompt opened no turn: the server refused it, or it took
    it and was gone before the state machine could."""
    if reply is not None and reply.ok:
        return AdapterError("the agent is not running", code="agent_down")
    return _refused(what, reply)


registry.register(OpencodeServeAdapter.adapter_id, OpencodeServeAdapter)
