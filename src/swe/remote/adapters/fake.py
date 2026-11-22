"""A scripted, in-process ``SessionAdapter`` for tests of the keeper and hub.

:class:`FakeAdapter` plays one script per turn: a list of steps built
with the helpers below (:func:`say`, :func:`think`, :func:`tool`,
:func:`ask`, :func:`ask_input`, :func:`fail`, :func:`exit_agent`,
:func:`task`, :func:`wait`, :func:`emit`). Its timestamps come from a
:class:`FakeClock` the test advances, so nothing sleeps. It records
every call it receives in ``calls`` (before it acts on it, so a call
that raises is recorded too).

By default a turn plays synchronously inside ``send_turn`` (and resumes
inside ``respond``) until it ends or reaches an :func:`ask`, where it
waits for the answer. With ``autoplay=False`` the test drives it one
step at a time with :meth:`FakeAdapter.step` or to the next stop with
:meth:`FakeAdapter.play`.

It is never registered by default; a test that resolves it through the
registry registers it (``registry.register("fake", FakeAdapter)``) and
unregisters it in cleanup.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from swe.remote.adapters import events as ev
from swe.remote.adapters import options as opts
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, EventSink, StartSpec, TranscriptPosition,
    TurnInput, UnknownRequest, check_option,
)


class FakeClock:
    """A clock that moves only when told. Call it for the time."""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("a clock does not run backwards")
        self.t += seconds
        return self.t


class ListSink:
    """An ``EventSink`` that keeps every event and checks each one
    against :func:`events.validate` as it arrives (raising, so a bad
    event fails the test at the line that emitted it).

    :meth:`wait_for` lets a test of an adapter that emits from its own
    thread wait for an event without polling.
    """

    def __init__(self, *, check: bool = True) -> None:
        self.events: list[dict] = []
        self.check = check
        self._cond = threading.Condition()

    def emit(self, event: dict) -> None:
        if self.check:
            problems = ev.validate(event)
            if problems:
                raise AssertionError(f"invalid {event.get('type')}: "
                                     + "; ".join(problems))
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def types(self) -> list[str]:
        with self._cond:
            return [e["type"] for e in self.events]

    def of(self, etype: str) -> list[dict]:
        with self._cond:
            return [e for e in self.events if e["type"] == etype]

    def clear(self) -> None:
        with self._cond:
            self.events.clear()

    def wait_for(self, match, timeout: float = 5.0) -> dict | None:
        """The first event ``match(event)`` accepts (a type name works
        too), waiting up to ``timeout`` seconds for it; None if none
        came."""
        if isinstance(match, str):
            etype = match
            match = lambda e: e.get("type") == etype  # noqa: E731
        found: list[dict] = []

        def seen() -> bool:
            found[:] = [e for e in self.events if match(e)][:1]
            return bool(found)

        with self._cond:
            self._cond.wait_for(seen, timeout)
        return found[0] if found else None


@dataclass(frozen=True)
class Step:
    """One scripted action. ``after`` seconds pass on the clock first."""

    op: str
    args: dict = field(default_factory=dict)
    after: float = 0.0


@dataclass(frozen=True)
class Call:
    """One call the adapter received."""

    name: str
    args: tuple = ()


# ---------------------------------------------------------------------
# Step builders


def say(text: str, *, chunks: Iterable[str] | None = None,
        stream: bool = True, after: float = 0.0) -> Step:
    """An assistant reply: deltas (``chunks``, default the whole text in
    one) then ``message.completed``. ``stream=False`` skips the deltas."""
    parts = list(chunks) if chunks is not None else [text]
    return Step("say", {"text": text, "chunks": parts if stream else []},
                after)


def think(text: str, *, duration_s: float | None = None,
          after: float = 0.0) -> Step:
    return Step("think", {"text": text, "duration_s": duration_s}, after)


def tool(name: str, *, input: Any = None, output: str = "",
         status: str = "ok", summary: str | None = None,
         file_edits: list | None = None, call_id: str | None = None,
         after: float = 0.0) -> Step:
    """``tool.started`` then ``tool.completed`` for one call."""
    return Step("tool", {"name": name, "input": input, "output": output,
                         "status": status, "summary": summary,
                         "file_edits": file_edits, "call_id": call_id},
                after)


def ask(prompt: str, *, kind: str = "tool", options: list | None = None,
        request_id: str | None = None, after: float = 0.0) -> Step:
    """An approval the turn waits on until ``respond`` answers it."""
    return Step("ask", {"prompt": prompt, "kind": kind,
                        "options": options, "request_id": request_id},
                after)


def ask_input(prompt: str, *, request_id: str | None = None,
              after: float = 0.0) -> Step:
    """A free-text question the turn waits on."""
    return Step("input", {"prompt": prompt, "request_id": request_id},
                after)


def task(task_id: str, status: str = "running", *, kind: str = "subagent",
         title: str | None = None, can_stop: bool = True,
         after: float = 0.0) -> Step:
    return Step("task", {"task_id": task_id, "status": status,
                         "kind": kind, "title": title,
                         "can_stop": can_stop}, after)


def fail(message: str, *, code: str = "agent_error",
         after: float = 0.0) -> Step:
    """The turn fails here: ``error`` then ``turn.ended`` failed."""
    return Step("fail", {"message": message, "code": code}, after)


def exit_agent(reason: str = "agent exited", *, after: float = 0.0) -> Step:
    """The agent process dies here (the contract's exit sequence)."""
    return Step("exit", {"reason": reason}, after)


def wait(seconds: float) -> Step:
    """Only moves the clock."""
    return Step("wait", {}, seconds)


def emit(etype: str, *, command_id: str | None = None, after: float = 0.0,
         **data: Any) -> Step:
    """Any event, built with :func:`events.make` (``turn_id`` is the
    current turn's)."""
    return Step("emit", {"type": etype, "data": data,
                         "command_id": command_id}, after)


# ---------------------------------------------------------------------


class _Turn:
    def __init__(self, turn_id: str, turn: TurnInput, steps: list[Step],
                 started: float) -> None:
        self.turn_id = turn_id
        self.input = turn
        self.steps = list(steps)
        self.started = started
        self.blocked = ""  # the request_id the turn waits on


class FakeAdapter:
    """See the module docstring. ``turns`` is one step list per
    ``send_turn``, in order; a turn past the end of it just ends."""

    adapter_id = "fake"

    def __init__(
        self,
        resolved: Any = None,
        *,
        turns: Iterable[Iterable[Step]] = (),
        caps: Caps | None = None,
        clock: FakeClock | None = None,
        session_id: str = "fake-session",
        autoplay: bool = True,
        position: TranscriptPosition | None = None,
        choices: dict | None = None,
        refuse_options: dict | None = None,
        offers_options: bool = True,
    ) -> None:
        if caps is None:
            caps = getattr(resolved, "caps", None) or Caps()
        self._caps = caps
        self.resolved = resolved
        self.clock = clock or FakeClock()
        self.session_id = session_id
        self.autoplay = autoplay
        self.position = position
        self.script = [list(t) for t in turns]
        self.calls: list[Call] = []
        self.options: dict[str, Any] = {}
        # ``session.options`` (G32): the values listed per option name
        # (``{name: [ids]}``), and options whose ``set_option`` the
        # "agent" refuses with the given message.
        self.choices = {k: list(v) for k, v in (choices or {}).items()}
        self.refuse_options = dict(refuse_options or {})
        # False: no ``session.options`` at all, as an adapter from before
        # G32 (the session-view fixtures, recorded against such a hub).
        self.offers_options = offers_options
        self._shown = opts.OptionsView()
        self.spec: StartSpec | None = None
        self.sink: EventSink | None = None
        self.alive = False
        self.closed = False
        self.turn: _Turn | None = None
        self.finished_turns: list[str] = []
        self.pending: dict[str, str] = {}  # request_id -> approval|input
        self.tasks: dict[str, dict] = {}
        self._n = {"turn": 0, "msg": 0, "req": 0, "call": 0}
        self._lock = threading.RLock()

    # -- helpers ---------------------------------------------------------

    @property
    def capabilities(self) -> Caps:
        return self._caps

    @property
    def key(self) -> str:
        return self.spec.session_key if self.spec else ""

    def _next(self, kind: str, prefix: str) -> str:
        self._n[kind] += 1
        return f"{prefix}{self._n[kind]}"

    def _send(self, event: dict) -> None:
        assert self.sink is not None
        self.sink.emit(event)

    def _tid(self) -> str | None:
        return self.turn.turn_id if self.turn else None

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append(Call(name, args))

    def _live(self) -> None:
        if self.spec is None:
            raise AdapterError("the session is not started",
                               code="not_started")
        if not self.alive:
            raise AdapterError("the agent is not running", code="agent_down")

    def _state(self, state: str, reason: str | None = None) -> None:
        self._send(ev.session_state(self.key, state, reason=reason,
                                    ts=self.clock()))

    def _retire(self, source: str) -> None:
        for rid in list(self.pending):
            del self.pending[rid]
            self._send(ev.approval_resolved(self.key, rid, source,
                                            turn_id=self._tid(),
                                            ts=self.clock()))

    def _end_turn(self, status: str, reason: str | None = None) -> None:
        turn = self.turn
        if turn is None:
            return
        self._send(ev.turn_ended(
            self.key, turn.turn_id, status, reason=reason,
            duration_s=round(self.clock() - turn.started, 3),
            command_id=turn.input.command_id or None, ts=self.clock()))
        self.finished_turns.append(turn.turn_id)
        self.turn = None

    # -- SessionAdapter --------------------------------------------------

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        with self._lock:
            self._record("start", spec)
            if self.closed:
                raise AdapterError("the adapter is closed", code="closed")
            if self.spec is not None:
                raise AdapterError("already started", code="started")
            if spec.fork:
                self._caps.require("fork")
            self.spec, self.sink, self.alive = spec, sink, True
            if spec.resume_id and not spec.fork:
                self.session_id = spec.resume_id
            elif spec.session_id:
                self.session_id = spec.session_id
            elif spec.fork:
                self.session_id = f"{spec.resume_id}-fork"
            for name in ("model", "effort", "permission_mode"):
                if getattr(spec, name):
                    self.options[name] = getattr(spec, name)
            if spec.plan_mode:
                self.options["plan_mode"] = True
            self._send(ev.session_caps(self.key, self._caps.as_dict(),
                                       ts=self.clock()))
            self._send(ev.session_state(
                self.key, "idle", harness_session_id=self.session_id,
                ts=self.clock()))
            self._options_changed()
            return self.session_id

    def _options_changed(self) -> None:
        if not self.offers_options:
            return
        shown = {}
        for name in ("model", "effort", "permission_mode"):
            listed = self.choices.get(name)
            shown[name] = opts.string_option(
                self.options.get(name, ""), opts.UNVERIFIED,
                choices=[opts.choice(c) for c in listed]
                if listed is not None else None)
        shown["plan_mode"] = opts.flag_option(
            self.options.get("plan_mode", False), opts.UNVERIFIED)
        event = self._shown.changed(self.key, shown, self._caps,
                                    self.clock())
        if event is not None:
            self._send(event)

    def agent_sets(self, name: str, value: Any) -> None:
        """The "agent" changes an option by itself (a fallback model, a
        plan mode it leaves): what a test of that path drives."""
        with self._lock:
            self.options[name] = value
            if self.alive:
                self._options_changed()

    def send_turn(self, turn: TurnInput) -> str:
        with self._lock:
            self._record("send_turn", turn)
            self._live()
            if self.turn is not None:
                raise AdapterError("a turn is already running", code="busy")
            steps = self.script.pop(0) if self.script else []
            turn_id = self._next("turn", "t")
            self.turn = _Turn(turn_id, turn, steps, self.clock())
            cid = turn.command_id or None
            self._send(ev.turn_started(self.key, turn_id,
                                       by=turn.device or None,
                                       command_id=cid, ts=self.clock()))
            self._state("running")
            self._send(ev.message_completed(
                self.key, self._next("msg", "m"), "user", turn.text,
                by=turn.device or None,
                attachments=list(turn.attachments) or None,
                turn_id=turn_id, command_id=cid, ts=self.clock()))
            if self.autoplay:
                self.play()
            return turn_id

    def steer(self, turn: TurnInput) -> None:
        with self._lock:
            self._record("steer", turn)
            self._live()
            self._caps.require("steer")
            if self.turn is None:
                raise AdapterError("no turn to steer", code="idle")
            self._send(ev.message_completed(
                self.key, self._next("msg", "m"), "user", turn.text,
                by=turn.device or None,
                attachments=list(turn.attachments) or None,
                turn_id=self.turn.turn_id,
                command_id=turn.command_id or None, ts=self.clock()))

    def interrupt(self) -> None:
        with self._lock:
            self._record("interrupt")
            self._live()
            if self.turn is None:
                return
            self._retire("turn_ended")
            self._end_turn("interrupted")
            self._state("idle")

    def respond(self, request_id: str, answer: Answer) -> None:
        with self._lock:
            self._record("respond", request_id, answer)
            self._live()
            if request_id not in self.pending:
                raise UnknownRequest(request_id)
            del self.pending[request_id]
            self._send(ev.approval_resolved(
                self.key, request_id, "user",
                decision=answer.decision or None,
                option_id=answer.option_id or None,
                text=answer.text or None, by=answer.device or None,
                turn_id=self._tid(), ts=self.clock()))
            if self.turn is not None and self.turn.blocked == request_id:
                self.turn.blocked = ""
                self._state("running")
                if self.autoplay:
                    self.play()

    def set_option(self, name: str, value: Any) -> None:
        with self._lock:
            self._record("set_option", name, value)
            self._live()
            check_option(self._caps, name, value)
            if name in self.refuse_options:
                raise AdapterError(self.refuse_options[name],
                                   code="refused")
            self.options[name] = value
            self._options_changed()

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            self._record("stop_task", task_id)
            self._live()
            self._caps.require("stop_task")
            known = self.tasks.get(task_id)
            if known is None:
                raise AdapterError(f"no task {task_id!r}",
                                   code="unknown_task")
            known["status"] = "stopped"
            self._send(ev.task_updated(self.key, task_id, known["kind"],
                                       "stopped",
                                       parent_turn_id=known["parent"],
                                       title=known["title"], can_stop=False,
                                       ts=self.clock()))

    def rollback(self, turn_id: str) -> None:
        with self._lock:
            self._record("rollback", turn_id)
            self._live()
            self._caps.require("rollback")
            if self.turn is not None:
                raise AdapterError("a turn is running", code="busy")
            if turn_id not in self.finished_turns:
                raise AdapterError(f"no turn {turn_id!r}",
                                   code="unknown_turn")
            at = self.finished_turns.index(turn_id)
            del self.finished_turns[at:]

    def transcript_position(self) -> TranscriptPosition | None:
        self._record("transcript_position")
        return self.position

    def close(self, grace_s: float = 2.0) -> None:
        with self._lock:
            self._record("close", grace_s)
            if self.closed:
                return
            self.closed = True
            if not self.alive:
                return
            self._retire("closed")
            self._end_turn("interrupted", "session closed")
            self._state("closed")
            self.alive = False

    # -- driving the script ----------------------------------------------

    def crash(self, reason: str = "agent exited") -> None:
        """The agent dies now, wherever the script is."""
        with self._lock:
            if not self.alive:
                return
            self._retire("agent_exit")
            self._end_turn("failed", reason)
            self._state("agent_down", reason)
            self.alive = False

    def step(self) -> bool:
        """Play one step of the current turn (ending the turn after its
        last). False when nothing can move: no turn, the turn waits on
        an answer, or the agent is down."""
        with self._lock:
            turn = self.turn
            if turn is None or turn.blocked or not self.alive:
                return False
            if not turn.steps:
                self._end_turn("completed")
                self._state("idle")
                return True
            step = turn.steps.pop(0)
            if step.after:
                self.clock.advance(step.after)
            getattr(self, f"_do_{step.op}")(turn, **step.args)
            return True

    def play(self) -> None:
        """Play steps until the turn ends, blocks or the agent dies."""
        while self.step():
            pass

    def _do_say(self, turn: _Turn, text: str, chunks: list) -> None:
        mid = self._next("msg", "m")
        offset = 0
        for i, chunk in enumerate(chunks, 1):
            self._send(ev.message_delta(
                self.key, mid, "assistant", chunk, i, offset=offset,
                done=i == len(chunks), turn_id=turn.turn_id,
                ts=self.clock()))
            offset += len(chunk)
        self._send(ev.message_completed(self.key, mid, "assistant", text,
                                        turn_id=turn.turn_id,
                                        ts=self.clock()))

    def _do_think(self, turn: _Turn, text: str,
                  duration_s: float | None) -> None:
        self._send(ev.thinking_completed(
            self.key, self._next("msg", "m"), text, duration_s=duration_s,
            turn_id=turn.turn_id, ts=self.clock()))

    def _do_tool(self, turn: _Turn, name: str, input: Any, output: str,
                 status: str, summary: str | None, file_edits: list | None,
                 call_id: str | None) -> None:
        call_id = call_id or self._next("call", "call-")
        self._send(ev.tool_started(self.key, call_id, name, summary=summary,
                                   input=input, turn_id=turn.turn_id,
                                   ts=self.clock()))
        self._send(ev.tool_completed(self.key, call_id, status, name=name,
                                     output=output, file_edits=file_edits,
                                     turn_id=turn.turn_id, ts=self.clock()))

    def _do_ask(self, turn: _Turn, prompt: str, kind: str,
                options: list | None, request_id: str | None) -> None:
        rid = request_id or self._next("req", "r")
        self.pending[rid] = "approval"
        turn.blocked = rid
        self._send(ev.approval_requested(self.key, rid, kind, prompt,
                                         options=options,
                                         turn_id=turn.turn_id,
                                         ts=self.clock()))
        self._state("waiting")

    def _do_input(self, turn: _Turn, prompt: str,
                  request_id: str | None) -> None:
        rid = request_id or self._next("req", "r")
        self.pending[rid] = "input"
        turn.blocked = rid
        self._send(ev.input_requested(self.key, rid, prompt,
                                      turn_id=turn.turn_id, ts=self.clock()))
        self._state("waiting")

    def _do_task(self, turn: _Turn, task_id: str, status: str, kind: str,
                 title: str | None, can_stop: bool) -> None:
        self.tasks[task_id] = {"kind": kind, "title": title,
                               "parent": turn.turn_id, "status": status}
        self._send(ev.task_updated(self.key, task_id, kind, status,
                                   parent_turn_id=turn.turn_id, title=title,
                                   can_stop=can_stop, ts=self.clock()))

    def _do_fail(self, turn: _Turn, message: str, code: str) -> None:
        self._send(ev.error(self.key, code, message, turn_id=turn.turn_id,
                            ts=self.clock()))
        self._retire("turn_ended")
        self._end_turn("failed", message)
        self._state("idle")

    def _do_exit(self, turn: _Turn, reason: str) -> None:
        self.crash(reason)

    def _do_wait(self, turn: _Turn) -> None:
        return

    def _do_emit(self, turn: _Turn, type: str, data: dict,
                 command_id: str | None) -> None:
        self._send(ev.make(type, self.key, data, turn_id=turn.turn_id,
                           command_id=command_id, ts=self.clock()))
