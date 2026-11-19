"""The harness adapter interface: one ``SessionAdapter`` per live session.

An adapter speaks one harness's structured protocol (stream-json, an
app-server, ``serve``, ACP) and turns it into canonical v2 events
(:mod:`swe.remote.adapters.events`). The keeper owns one adapter per
session; nothing above the adapter knows which harness it drives.

The contract every adapter keeps
================================

Threading
    Every method on :class:`SessionAdapter` is called from one thread,
    the keeper's command loop, and never concurrently with another
    method of the same adapter. Events go out through the
    :class:`EventSink` from whatever thread the adapter reads its agent
    on (usually its own reader thread). ``sink.emit`` is thread-safe and
    never calls back into the adapter, so an adapter may emit while it
    holds its own lock.

Blocking
    ``start`` may block until the harness answers its handshake (bounded
    by the adapter's own timeout, about 60 s at most for a resume that
    replays history). Every other call returns once the harness has
    *accepted* the request, never when the work it starts finishes:
    ``send_turn`` returns when the turn is under way, not when it ends.
    Each blocking call is bounded; an adapter that times out raises
    :class:`AdapterError` rather than hanging the keeper's loop.
    ``close`` blocks for at most ``grace_s`` plus the time to kill.

Ordering
    Events are emitted in the order they happened. An adapter emits from
    one thread at a time (one reader thread, or a lock around ``emit``),
    so the sink sees one session's events in a single order. Within a
    turn: ``turn.started`` first, ``turn.ended`` last. A
    ``message.delta`` for a message always precedes that message's
    ``message.completed``; an ``approval.requested`` precedes its
    ``approval.resolved``; ``tool.started`` precedes ``tool.updated``
    and ``tool.completed`` for the same ``call_id``.

Agent exit
    When the agent process ends, for any reason other than ``close``,
    the adapter emits, exactly once and in this order: an
    ``approval.resolved`` with ``source: "agent_exit"`` for every open
    approval or input request, ``turn.ended`` with ``status: "failed"``
    for an open turn, then ``session.state`` with ``state:
    "agent_down"``. After ``close`` it emits the same retirements with
    ``source: "closed"`` and ``status: "interrupted"`` and ends with
    ``session.state`` ``closed``. Nothing is emitted after either; later
    calls raise :class:`AdapterError` with ``code: "agent_down"``.

Capabilities
    Optional methods (``steer``, ``stop_task``, ``rollback``, plan mode
    and the other ``set_option`` names, forking in ``start``) are gated
    by :class:`Caps`, never by harness name. A caller checks
    ``adapter.capabilities`` first; an adapter asked for something its
    caps do not allow raises :class:`Unsupported`. ``capabilities`` may
    narrow after ``start`` (the harness's own handshake can say less
    than harness.json promised); the adapter then emits ``session.caps``.

Redaction
    Tool input and output, approval prompts and message text are
    redacted (``reports.transcripts._redact_secrets``) by the adapter
    before they are emitted: the events go to disk and to phones as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Protocol, runtime_checkable

# ``set_option`` names. ``plan_mode`` is gated by ``Caps.plan_mode``,
# the rest by ``Caps.options``.
OPTION_NAMES = frozenset({"model", "effort", "plan_mode", "permission_mode"})

# The features a ``Caps`` can switch on, by the name ``Caps.supports``
# and ``Unsupported.feature`` use.
FEATURES = ("steer", "plan_mode", "rollback", "fork", "stop_task")

# Approval decisions an ``Answer`` may carry, as v1 already spells them.
DECISIONS = frozenset({"allow", "deny", "always"})


class AdapterError(Exception):
    """A request the adapter could not carry out. ``code`` is a short
    machine-readable reason (``agent_down``, ``timeout``, ``refused``,
    ``not_started``...); the message is for a person."""

    def __init__(self, message: str, *, code: str = "adapter_error") -> None:
        super().__init__(message)
        self.code = code


class Unsupported(AdapterError):
    """The adapter's capabilities do not include ``feature``."""

    def __init__(self, feature: str, message: str = "") -> None:
        super().__init__(message or f"{feature} is not supported here",
                         code="unsupported")
        self.feature = feature


class UnknownRequest(AdapterError):
    """``respond`` named a request that is not open (never asked, already
    answered, or retired at turn end)."""

    def __init__(self, request_id: str) -> None:
        super().__init__(f"no open request {request_id!r}",
                         code="unknown_request")
        self.request_id = request_id


@dataclass(frozen=True)
class Caps:
    """What one adapter can do for one session.

    The keys mirror harness.json's ``capabilities.session``; ``options``
    lists the ``set_option`` names other than ``plan_mode`` the channel
    can change mid-session (``model``, ``effort``, ``permission_mode``).
    Everything defaults to off: a feature is on only where it is proven.
    """

    steer: bool = False
    plan_mode: bool = False
    rollback: bool = False
    fork: bool = False
    stop_task: bool = False
    options: frozenset = frozenset()

    def __post_init__(self) -> None:
        opts = frozenset(self.options)
        unknown = opts - (OPTION_NAMES - {"plan_mode"})
        if unknown:
            raise ValueError(f"unknown option names: {sorted(unknown)}")
        object.__setattr__(self, "options", opts)

    def supports(self, feature: str) -> bool:
        """Whether ``feature`` (one of ``FEATURES``) is on."""
        if feature not in FEATURES:
            raise ValueError(f"unknown feature {feature!r}")
        return bool(getattr(self, feature))

    def can_set(self, name: str) -> bool:
        """Whether ``set_option(name, ...)`` is allowed."""
        if name == "plan_mode":
            return self.plan_mode
        return name in self.options

    def require(self, feature: str) -> None:
        """Raise :class:`Unsupported` unless ``feature`` is on."""
        if not self.supports(feature):
            raise Unsupported(feature)

    def narrowed(self, other: "Caps") -> "Caps":
        """Only what both ``self`` and ``other`` allow: harness.json's
        ceiling met with what the harness's handshake reported."""
        return Caps(**{f: getattr(self, f) and getattr(other, f)
                       for f in FEATURES},
                    options=self.options & other.options)

    def as_dict(self) -> dict:
        """The JSON shape ``session.caps`` carries."""
        out: dict[str, Any] = {f.name: getattr(self, f.name)
                               for f in fields(self) if f.name != "options"}
        out["options"] = sorted(self.options)
        return out


@dataclass(frozen=True)
class StartSpec:
    """How to start or resume one session.

    ``resume_id`` empty starts a new session; set, it resumes that
    harness session, or forks from it when ``fork`` is true (needs
    ``Caps.fork``). ``argv`` is the resolved command from
    :func:`swe.remote.adapters.registry.adapter_for`; None lets the
    adapter use its own default. ``harness`` is for labels and logs
    only: an adapter never branches on it.
    """

    session_key: str
    cwd: str
    argv: tuple | None = None
    resume_id: str = ""
    fork: bool = False
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    plan_mode: bool = False
    env: Mapping[str, str] = field(default_factory=dict)
    harness: str = ""

    def __post_init__(self) -> None:
        if not self.session_key:
            raise ValueError("session_key is required")
        if not self.cwd.startswith("/"):
            raise ValueError("cwd must be an absolute path")
        if self.fork and not self.resume_id:
            raise ValueError("fork needs a resume_id to fork from")
        if self.argv is not None:
            argv = tuple(self.argv)
            if not argv or not all(isinstance(t, str) and t for t in argv):
                raise ValueError("argv must be non-empty strings")
            object.__setattr__(self, "argv", argv)


@dataclass(frozen=True)
class TurnInput:
    """One user message for ``send_turn`` or ``steer``.

    ``attachments`` are staged file refs (absolute paths on this
    machine). ``command_id`` is the client's id for the write, echoed on
    the turn's events; ``device`` is the id of the device that sent it.
    """

    text: str
    attachments: tuple = ()
    command_id: str = ""
    device: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        object.__setattr__(self, "attachments", tuple(self.attachments))


@dataclass(frozen=True)
class Answer:
    """A reply to an ``approval.requested`` or ``input.requested``.

    ``decision`` is ``allow``, ``deny`` or ``always``; ``option_id``
    names one of the request's own options instead; ``text`` answers a
    question or input request, or adds feedback to a plan decision.
    ``device`` is who answered (the ``by`` of ``approval.resolved``).
    """

    decision: str = ""
    option_id: str = ""
    text: str = ""
    device: str = ""

    def __post_init__(self) -> None:
        if self.decision and self.decision not in DECISIONS:
            raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
        if not (self.decision or self.option_id or self.text):
            raise ValueError("an answer needs a decision, option_id or text")


@dataclass(frozen=True)
class TranscriptPosition:
    """Where the harness's own transcript stood when last read: its path
    and byte size. A later size past ``offset`` while the session is
    parked means someone else wrote to it (a terminal resume)."""

    path: str
    offset: int


@runtime_checkable
class EventSink(Protocol):
    """Where an adapter's events go (the keeper's event log).

    ``emit`` takes one v2 envelope as built by
    :mod:`swe.remote.adapters.events`, with ``seq`` 0; the sink stamps
    the real ``seq``. It is thread-safe, returns quickly, and never
    calls back into the adapter.
    """

    def emit(self, event: dict) -> None: ...


@runtime_checkable
class SessionAdapter(Protocol):
    """One harness session driven over its structured protocol.

    See the module docstring for the threading, blocking, ordering and
    exit contract. ``adapter_id`` is the id it registers under
    (``registry.register``) and harness.json names in
    ``capabilities.session.adapter``.
    """

    adapter_id: str

    @property
    def capabilities(self) -> Caps:
        """The caps in force now (may narrow after ``start``)."""
        ...

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        """Start or resume the session; return the harness's own session
        id (the new one for a fork). Emits ``session.caps`` then
        ``session.state`` before returning."""
        ...

    def send_turn(self, turn: TurnInput) -> str:
        """Start a turn with ``turn``; return its ``turn_id``. Raises
        :class:`AdapterError` (``code: "busy"``) mid-turn: queueing is
        the keeper's job, not the adapter's."""
        ...

    def steer(self, turn: TurnInput) -> None:
        """Add ``turn`` (its text and its attachments) to the turn in
        flight; emits that user ``message.completed`` with the turn's
        ``attachments``. Gated by ``Caps.steer``."""
        ...

    def interrupt(self) -> None:
        """Stop the turn in flight; a no-op when idle. Open requests are
        retired with ``source: "turn_ended"``."""
        ...

    def respond(self, request_id: str, answer: Answer) -> None:
        """Answer an open approval or input request. Raises
        :class:`UnknownRequest` for one that is not open."""
        ...

    def set_option(self, name: str, value: Any) -> None:
        """Change ``model``, ``effort``, ``plan_mode`` or
        ``permission_mode`` for the turns after this one. Gated by
        ``Caps.can_set``."""
        ...

    def stop_task(self, task_id: str) -> None:
        """Stop one subagent or background task. Gated by
        ``Caps.stop_task``."""
        ...

    def rollback(self, turn_id: str) -> None:
        """Rewind the conversation to before ``turn_id`` (files are the
        keeper's checkpoints, not this). Gated by ``Caps.rollback``."""
        ...

    def transcript_position(self) -> TranscriptPosition | None:
        """Where the harness transcript stands, or None when the harness
        writes none this adapter can name."""
        ...

    def close(self, grace_s: float = 2.0) -> None:
        """End the session: give the agent ``grace_s`` to exit, then
        kill it. Idempotent. A closed adapter is never started again:
        a later ``start`` raises :class:`AdapterError`
        (``code: "closed"``); make a new adapter instead."""
        ...


# Every method a SessionAdapter has, for conformance checks.
METHODS = (
    "start", "send_turn", "steer", "interrupt", "respond", "set_option",
    "stop_task", "rollback", "transcript_position", "close",
)


def missing_methods(obj: object) -> list[str]:
    """The ``SessionAdapter`` members ``obj`` lacks or has non-callable;
    empty when it conforms structurally."""
    missing = [name for name in METHODS
               if not callable(getattr(obj, name, None))]
    if not isinstance(getattr(obj, "adapter_id", None), str):
        missing.append("adapter_id")
    try:
        caps = getattr(obj, "capabilities")
    except Exception:
        caps = None
    if not isinstance(caps, Caps):
        missing.append("capabilities")
    return missing


def check_option(caps: Caps, name: str, value: Any) -> None:
    """Validate one ``set_option`` call against ``caps``: raises
    ValueError for an unknown name or a wrong value type and
    :class:`Unsupported` for a name ``caps`` does not allow."""
    if name not in OPTION_NAMES:
        raise ValueError(f"unknown option {name!r}")
    if name == "plan_mode":
        if not isinstance(value, bool):
            raise ValueError("plan_mode must be true or false")
    elif not isinstance(value, str) or not value or any(
            ch.isspace() for ch in value):
        raise ValueError(f"{name} must be one short token")
    if not caps.can_set(name):
        raise Unsupported(name)
