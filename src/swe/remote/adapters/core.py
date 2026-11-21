"""What the G16 adapters' state machines share (Codex, opencode, ACP).

``SessionCore`` keeps one session's protocol-independent state (the
open turn, the open requests, the live text streams) and builds the v2
events the contract in ``base.py`` fixes for every adapter: the start
pair, a turn's opening, the user's message, requests and their
retirement, redacted live text, and the two exit sequences. A wire
module subclasses it and adds only what its harness's messages mean.

``LineWriter`` is the one thread that writes a child's stdin, so a
child that stops reading can stall neither a caller nor the reader.

Redaction is the Claude adapter's (``claude_wire.redact`` and
``stable_cut``), imported as is: one rule for what a secret looks like,
whichever harness printed it.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from swe.remote.adapters import events as ev
from swe.remote.adapters import options as opts
from swe.remote.adapters.base import Answer, Caps, UnknownRequest
from swe.remote.adapters.claude_wire import (  # noqa: F401 - re-exported
    OUTPUT_LIMIT, REDACTED, redact, secret_key, stable_cut, tool_summary,
)

# Every feature on: the ceiling a fixture replay starts from.
ALL_CAPS = Caps(steer=True, plan_mode=True, rollback=True, fork=True,
                stop_task=True, options=frozenset({"model", "effort",
                                                   "permission_mode"}))

# The epoch a fixture's relative times are replayed on.
FIXTURE_T0 = 1_760_000_000.0
FIXTURE_SESSION = "fixture:1"


def text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


def jsonable(value: Any) -> Any:
    """``value`` as strict JSON: a non-finite number or anything JSON
    has no type for becomes its string."""
    if isinstance(value, float) and (value != value or value in (
            float("inf"), float("-inf"))):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Tool output cut to what an event carries."""
    return text if len(text) <= limit else text[:limit] + "\n…"


def count(value: Any) -> int | None:
    """A token count: a non-negative int, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


@dataclass
class Turn:
    turn_id: str
    started: float
    command_id: str = ""
    interrupted: bool = False
    error: bool = False
    reason: str = ""
    user_msgs: int = 0
    # The harness's own id for this turn, once it has said it.
    native_id: str = ""


@dataclass
class Stream:
    """One streamed message: everything received (``raw``) and the
    redacted prefix already sent (``shown``)."""

    id: str
    kind: str
    seq: int = 0
    raw: str = ""
    shown: str = ""


@dataclass
class Ack:
    """The harness's answer to one of our requests."""

    request_id: Any
    ok: bool
    body: dict = field(default_factory=dict)
    error: str = ""
    code: Any = None


@dataclass
class Fed:
    """What one harness message came to: events to emit, messages to
    write back, and acks for our own requests."""

    events: list = field(default_factory=list)
    replies: list = field(default_factory=list)
    acks: list = field(default_factory=list)


class SessionCore:
    """See the module docstring. Every method that returns a list
    returns v2 events in emission order; nothing is returned once the
    session is over (``done``)."""

    def __init__(self, session: str, *, clock: Callable[[], float] = time.time,
                 caps: Caps = Caps(), session_id: str = "",
                 run_id: str = "") -> None:
        self.key = session
        # Turn ids are ``t-<run>-<n>``: one session's log spans many
        # starts of its adapter, and ids must not repeat in it.
        self.run_id = run_id or secrets.token_hex(4)
        self.clock = clock
        self.caps = caps
        self.session_id = session_id
        self.started = False
        self.done = False
        self.turn: Turn | None = None
        self.waiting = False
        # request id -> whatever the wire module needs to answer it
        self.pending: dict[str, Any] = {}
        self.streams: dict[str, Stream] = {}
        self._completed: set = set()
        self._held: list[dict] = []
        self._n_turn = 0
        # Message kinds nothing maps, by name: for tests and logs.
        self.unknown: dict[str, int] = {}
        self._options = opts.OptionsView()

    # -- helpers ---------------------------------------------------------

    def tid(self) -> str | None:
        return self.turn.turn_id if self.turn else None

    def now(self) -> float:
        return self.clock()

    def out(self, out: list, event: dict) -> None:
        """Queue ``event``; before ``start_events`` it is held back, so
        the start pair always comes first."""
        if not self.started:
            self._held.append(event)
        else:
            out.append(event)

    def options(self) -> dict:
        """Every option this harness could offer now, as
        ``session.options`` carries it (G32). A wire module overrides
        it; :meth:`sync_options` keeps the ones the caps allow."""
        return {}

    def sync_options(self, out: list) -> None:
        """Queue ``session.options`` when what :meth:`options` says
        changed since the last one: call it after anything that may
        change a value."""
        if not self.started or self.done:
            return
        event = self._options.changed(self.key, self.options(), self.caps,
                                      self.now())
        if event is not None:
            out.append(event)

    def options_events(self) -> list:
        """:meth:`sync_options` as a list, for the adapter to emit."""
        out: list = []
        self.sync_options(out)
        return out

    def note_unknown(self, name: Any) -> None:
        key = str(name)[:60]
        self.unknown[key] = self.unknown.get(key, 0) + 1

    def state(self, out: list, state: str, reason: str | None = None) -> None:
        self.out(out, ev.session_state(
            self.key, state, reason=redact(reason) if reason else None,
            ts=self.now()))

    def retire(self, out: list, source: str) -> None:
        for rid in list(self.pending):
            del self.pending[rid]
            self.out(out, ev.approval_resolved(self.key, rid, source,
                                               turn_id=self.tid(),
                                               ts=self.now()))
        self.waiting = False

    def open_turn(self, out: list, *, by: str = "", text: str = "",
                  command_id: str = "") -> Turn:
        self._n_turn += 1
        turn = Turn(f"t-{self.run_id}-{self._n_turn}", self.now(),
                    command_id=command_id)
        self.turn = turn
        self.out(out, ev.turn_started(
            self.key, turn.turn_id, by=by or None,
            text=redact(text) if text else None,
            command_id=command_id or None, ts=self.now()))
        self.state(out, "running")
        return turn

    def end_turn(self, out: list, status: str,
                 reason: str | None = None) -> None:
        turn = self.turn
        if turn is None:
            return
        self.out(out, ev.turn_ended(
            self.key, turn.turn_id, status,
            reason=redact(reason) if reason else None,
            duration_s=round(max(0.0, self.now() - turn.started), 3),
            command_id=turn.command_id or None, ts=self.now()))
        self.turn = None
        self.streams.clear()
        self._completed.clear()
        self.turn_cleared()

    def turn_cleared(self) -> None:
        """A hook: the turn just ended; drop per-turn wire state."""

    def finish_turn(self, out: list, status: str | None = None,
                    reason: str | None = None) -> None:
        """The harness says the turn is over: close what streams, retire
        what is open, ``turn.ended`` then ``session.state`` idle.
        ``status`` None reads it off the turn (interrupted if we
        interrupted, failed if it errored, else completed)."""
        turn = self.turn
        if turn is None:
            return
        self.close_streams(out)
        self.retire(out, "turn_ended")
        if status is None:
            status = "interrupted" if turn.interrupted else (
                "failed" if turn.error else "completed")
        self.end_turn(out, status, reason or turn.reason or None)
        self.state(out, "idle")

    # -- calls from the adapter ------------------------------------------

    def start_events(self, caps: Caps, session_id: str) -> list:
        """``session.caps``, ``session.state`` idle and
        ``session.options``, then anything the harness sent before the
        handshake finished."""
        self.caps = caps
        self.session_id = session_id
        out = [ev.session_caps(self.key, caps.as_dict(), ts=self.now()),
               ev.session_state(self.key, "idle",
                                harness_session_id=session_id or None,
                                ts=self.now())]
        self.started = True
        self.sync_options(out)
        out.extend(self._held)
        self._held = []
        return out

    def narrow(self, out: list, told: Caps) -> None:
        """The harness said less than the caps in force: narrow them and
        say so."""
        caps = self.caps.narrowed(told)
        if caps != self.caps:
            self.caps = caps
            self.out(out, ev.session_caps(self.key, caps.as_dict(),
                                          ts=self.now()))
            self.sync_options(out)

    def begin_turn(self, text: str, *, attachments: Iterable[str] = (),
                   command_id: str = "", device: str = "") -> tuple[str, list]:
        """A turn we start: ``turn.started``, ``session.state`` running
        and the user's ``message.completed``."""
        out: list = []
        turn = self.open_turn(out, by=device, text=text,
                              command_id=command_id)
        out.append(self.user_message(text, attachments=attachments,
                                     command_id=command_id, device=device))
        return turn.turn_id, out

    def user_message(self, text: str, *, attachments: Iterable[str] = (),
                     command_id: str = "", device: str = "") -> dict:
        assert self.turn is not None
        self.turn.user_msgs += 1
        n = self.turn.user_msgs
        mid = f"{self.turn.turn_id}:user" + (str(n) if n > 1 else "")
        return ev.message_completed(
            self.key, mid, "user", redact(text), by=device or None,
            attachments=[redact(str(a)) for a in attachments] or None,
            turn_id=self.turn.turn_id, command_id=command_id or None,
            ts=self.now())

    def interrupt_sent(self) -> list:
        """We asked the harness to stop the turn: its open requests are
        retired now, and the turn ends ``interrupted`` when the harness
        says it is over."""
        out: list = []
        if self.turn is None:
            return out
        self.turn.interrupted = True
        if self.pending:
            self.retire(out, "turn_ended")
            self.state(out, "running")
        return out

    def ask(self, out: list, request_id: str, kind: str, prompt: str,
            payload: Any, *, options: list | None = None,
            call_id: str = "") -> None:
        """An approval the harness waits on: ``approval.requested`` then
        ``session.state`` waiting. ``payload`` is kept under the id for
        the wire module's answer."""
        self.pending[request_id] = payload
        self.out(out, ev.approval_requested(
            self.key, request_id, kind, redact(prompt) or "approval needed",
            options=options or None, call_id=call_id or None,
            turn_id=self.tid(), ts=self.now()))
        self._wait(out)

    def ask_input(self, out: list, request_id: str, prompt: str,
                  payload: Any, *, options: list | None = None) -> None:
        """A question the harness waits on: ``input.requested``."""
        self.pending[request_id] = payload
        self.out(out, ev.input_requested(
            self.key, request_id, redact(prompt) or "input needed",
            kind="choice" if options else "text", options=options or None,
            turn_id=self.tid(), ts=self.now()))
        self._wait(out)

    def _wait(self, out: list) -> None:
        if not self.waiting:
            self.waiting = True
            self.state(out, "waiting")

    def take(self, request_id: str) -> Any:
        """The payload of an open request; raises UnknownRequest."""
        if request_id not in self.pending:
            raise UnknownRequest(request_id)
        return self.pending[request_id]

    def answered(self, request_id: str, answer: Answer,
                 decision: str = "") -> list:
        """We answered ``request_id``: ``approval.resolved`` (``user``),
        and ``session.state`` running once nothing else is open."""
        self.pending.pop(request_id, None)
        out = [ev.approval_resolved(
            self.key, request_id, "user", decision=decision or None,
            option_id=answer.option_id or None,
            text=redact(answer.text) if answer.text else None,
            by=answer.device or None, turn_id=self.tid(), ts=self.now())]
        self._resume(out)
        return out

    def reopen(self, request_id: str, payload: Any) -> None:
        """Our answer to ``request_id`` never reached the harness: the
        request is open again, and the session waits on it as before.
        (The events ``answered`` returned are the caller's to drop.)"""
        self.pending[request_id] = payload
        if self.turn is not None:
            self.waiting = True

    def withdrawn(self, out: list, request_id: str,
                  source: str = "harness") -> bool:
        """The harness took back a request it asked."""
        if request_id not in self.pending:
            return False
        del self.pending[request_id]
        self.out(out, ev.approval_resolved(self.key, request_id, source,
                                           turn_id=self.tid(),
                                           ts=self.now()))
        self._resume(out)
        return True

    def _resume(self, out: list) -> None:
        if not self.pending and self.waiting:
            self.waiting = False
            if self.turn is not None:
                self.state(out, "running")

    def exited(self, reason: str) -> list:
        """The agent exited on its own: the contract's exit sequence."""
        return self._finish("agent_exit", "failed", "agent_down", reason)

    def closed(self) -> list:
        return self._finish("closed", "interrupted", "closed", None,
                            turn_reason="session closed")

    def _finish(self, source: str, status: str, state: str,
                reason: str | None, turn_reason: str | None = None) -> list:
        if self.done:
            return []
        out: list = []
        self.started = True
        self._held = []
        self.retire(out, source)
        self.end_turn(out, status, turn_reason or reason)
        self.state(out, state, reason)
        self.done = True
        return out

    # -- live text -------------------------------------------------------

    def stream(self, sid: str, kind: str) -> Stream:
        block = self.streams.get(sid)
        if block is None:
            block = self.streams[sid] = Stream(sid, kind)
        return block

    def delta(self, out: list, sid: str, kind: str, text: str) -> None:
        """More text for stream ``sid``: a ``message.delta`` with the
        part of it whose redaction can no longer change."""
        if not sid or not isinstance(text, str) or not text \
                or sid in self._completed:
            return
        block = self.stream(sid, kind)
        block.raw += text
        self._show(out, block, redact(block.raw[:stable_cut(block.raw)]))

    def _show(self, out: list, block: Stream, shown: str,
              done: bool = False) -> None:
        """Emit what of ``shown`` is new. Text is never taken back: a
        redaction that no longer extends what went out waits (the
        completed message has it)."""
        if len(shown) <= len(block.shown) or not shown.startswith(
                block.shown):
            return
        block.seq += 1
        self.out(out, ev.message_delta(
            self.key, block.id, block.kind, shown[len(block.shown):],
            block.seq, offset=len(block.shown), done=done,
            turn_id=self.tid(), ts=self.now()))
        block.shown = shown

    def complete(self, out: list, sid: str, kind: str, text: str,
                 **fields: Any) -> None:
        """Stream ``sid`` is whole: its last delta (what of the redacted
        text has not gone out), then ``message.completed`` (``kind``
        assistant) or ``thinking.completed`` (``kind`` thinking). A
        second completion of one id is dropped."""
        if not sid or sid in self._completed:
            return
        final = redact(text)
        block = self.streams.get(sid)
        if block is not None:
            self._show(out, block, final, done=True)
        self._completed.add(sid)
        if kind == "thinking":
            self.out(out, ev.thinking_completed(
                self.key, sid, final, turn_id=self.tid(), ts=self.now(),
                **fields))
        else:
            self.out(out, ev.message_completed(
                self.key, sid, "assistant", final, turn_id=self.tid(),
                ts=self.now()))

    def close_streams(self, out: list) -> None:
        """Complete every stream a client has deltas of but no
        completion (a reply cut short): the text so far, redacted, as
        its last delta and its completion."""
        for block in list(self.streams.values()):
            if block.seq == 0 and not block.raw.strip():
                continue
            self.complete(out, block.id, block.kind, block.raw)


class Stalled(Exception):
    """A write did not reach the pipe in time: the child stopped
    reading."""


class LineWriter:
    """The one thread that writes a child's stdin.

    A pipe write blocks while the pipe is full, so no caller ever writes
    itself: ``send`` queues the bytes and waits, bounded, for them to
    reach the pipe; a reader thread's own replies are queued without
    waiting. Once a write misses its deadline the writer counts as
    stalled and takes nothing more; the adapter then kills the child,
    which ends the blocked write."""

    def __init__(self, stream: Any, name: str = "adapter-stdin") -> None:
        self._stream = stream
        self._queue: queue.Queue = queue.Queue()
        self.broken = False
        self.stalled = False
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=name)
        self.thread.start()

    def send(self, data: bytes | None, timeout: float | None) -> bool:
        """Queue ``data`` (None: close stdin). With a ``timeout``, wait
        that long for it to be written: True when it was, False when the
        pipe is gone; raises ``Stalled`` past the deadline. Without one,
        True once queued."""
        if self.broken or self.stalled:
            return False
        done = threading.Event()
        result = [False]
        self._queue.put((data, done, result))
        if timeout is None:
            return True
        if not done.wait(timeout):
            self.stalled = True
            raise Stalled()
        return result[0]

    def _run(self) -> None:
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
        close_quietly(self._stream)
        done.set()
        while True:
            try:
                _data, left, _result = self._queue.get_nowait()
            except queue.Empty:
                return
            left.set()

    def stop(self, timeout: float) -> None:
        """Make sure the thread ends: a stop message when it is idle,
        then a bounded join."""
        if not self.stalled and not self.broken:
            self._queue.put((None, threading.Event(), [False]))
        self.thread.join(timeout)


def close_quietly(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass
