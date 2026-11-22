"""The opencode ``serve`` wire format: the only module that knows it.

``opencode serve`` (2.x) is an HTTP server. Everything a session needs
is under ``/api``, behind HTTP Basic auth (user ``opencode``, the
password in ``OPENCODE_SERVER_PASSWORD``, which the adapter sets for
the child it starts):

- requests we make (:class:`Request`: a method, a path, a JSON body):
  ``POST /api/session``, ``POST /api/session/<id>/prompt``,
  ``/interrupt``, ``/permission/<request>/reply``, ``/model``,
  ``/agent``, ``/fork``, ``/revert/stage`` and ``/revert/commit``;
- one server-sent event stream, ``GET /api/event``, that carries every
  session's events as ``{"type", "data": {"sessionID", ...}}``:
  ``session.execution.started``, ``session.text.delta``,
  ``session.tool.called``, ``permission.asked``,
  ``session.execution.succeeded``...

Everything here is pure: builders for the requests, :class:`SseParser`
for the stream's lines, and :class:`ServeState`, which turns the
server's events and answers into v2 events (``events.py``). No
process, no socket, no clock of its own, so a recorded fixture replays
through the same code the live adapter runs (:func:`replay`).
``opencode_serve.py`` owns the process and the connections.

The shapes were read off the server's own ``/openapi.json`` and
recordings of the real CLI
(tests/fixtures/protocols/opencode_serve/); see
docs/specs/G16-more-adapters.md for what each fixture proves.

The 1.x server spoke a different API (``/session/<id>/prompt_async``,
no ``/api`` prefix, no auth). ``inject.OpencodeServeInjector``, the
pre-keeper channel, still speaks that one; its paths and event names
are at the end of this module.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, NamedTuple

from swe.remote.adapters import events as ev
from swe.remote.adapters import options as opts
from swe.remote.adapters.base import Answer, Caps
from swe.remote.adapters.claude_wire import parse_version, version_ok
from swe.remote.adapters.core import (
    ALL_CAPS, FIXTURE_SESSION, FIXTURE_T0, Fed, SessionCore, clip, count,
    jsonable, redact, text_of, tool_summary,
)

__all__ = [
    "ServeState", "SseParser", "Request", "replay", "PROVEN", "MIN_VERSION",
    "RECORDED_VERSION", "DEFAULT_ARGV", "DECISIONS", "parse_version",
    "version_ok",
]

# The command that starts the server; harness.json's
# ``capabilities.session.argv`` replaces it. The adapter appends the
# loopback bind and the port.
DEFAULT_ARGV = ("opencode", "serve")
BIND_ARGS = ("--hostname", "127.0.0.1", "--port", "0")

# The CLI version the fixtures were recorded against, and the oldest one
# ``start`` accepts when harness.json names no ``min_version``: 1.x
# serves another API, and nothing between was ever run against this
# module.
RECORDED_VERSION = "2.3.2"
MIN_VERSION = "2.3.2"

# The caps this module can deliver, each proven by a recording.
# ``stop_task`` has no request and stays off whatever harness.json says;
# opencode has no session-wide permission mode.
# ``steer_effect``: nothing is cut; the steer is delivered when the
# current step ends, the reply (steer) or a running command (steer_tool).
PROVEN = Caps(steer=True, plan_mode=True, rollback=True, fork=True,
              options=frozenset({"model", "effort"}),
              steer_effect="after_tool")

# Basic auth: the user name is fixed, the password is the child's
# ``PASSWORD_ENV``.
AUTH_USER = "opencode"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"

# The line the server prints once it accepts connections.
LISTENING_RE = re.compile(r"server listening on (http://[^\s]+)")

# Our decisions as permission replies.
DECISIONS = {"allow": "once", "always": "always", "deny": "reject"}

# The agents plan mode switches between.
PLAN_AGENT = "plan"
BUILD_AGENT = "build"

_SESSION_ID_RE = re.compile(r"ses_[A-Za-z0-9]{1,64}")


def is_session_id(value: Any) -> bool:
    """Whether ``value`` can name an opencode session (``ses_...``)."""
    return isinstance(value, str) and bool(_SESSION_ID_RE.fullmatch(value))


def listening_url(line: Any) -> str:
    """The base URL in the server's "listening" line, or ""."""
    m = LISTENING_RE.search(line) if isinstance(line, str) else None
    return m.group(1).rstrip("/") if m else ""


def new_password() -> str:
    """A password for one server start. opencode hands its environment
    to the commands its agent runs, so the agent can print this; it is
    shaped like an API key (``sk-`` and 48 hex digits) so the redactor
    every event goes through masks it wherever it turns up, live text
    included."""
    return "sk-" + secrets.token_hex(24)


def basic_auth(password: str) -> str:
    """The ``Authorization`` header value."""
    token = base64.b64encode(f"{AUTH_USER}:{password}".encode("utf-8"))
    return "Basic " + token.decode("ascii")


def model_ref(model: str, effort: str = "") -> dict | None:
    """``provider/model`` as the server's ``{providerID, id, variant}``;
    None for a name with no provider (the server would not find it)."""
    provider, sep, name = (model or "").partition("/")
    if not sep or not provider or not name:
        return None
    ref = {"providerID": provider, "id": name}
    if effort:
        ref["variant"] = effort
    return ref


def model_name(ref: Any) -> str:
    """A server model ref back as ``provider/model``."""
    if isinstance(ref, dict) and text_of(ref.get("providerID")) \
            and text_of(ref.get("id")):
        return f"{ref['providerID']}/{ref['id']}"
    return ""


# ---------------------------------------------------------------------
# Requests we make


class Request(NamedTuple):
    method: str
    path: str
    body: dict | None = None


def seg(value: Any) -> str:
    """``value`` as one URL path segment: an id from a client must not
    be able to name another route."""
    return urllib.parse.quote(str(value), safe="")


def _session(session_id: str, tail: str = "") -> str:
    return f"/api/session/{seg(session_id)}{tail}"


def info() -> Request:
    return Request("GET", "/api/info")


def events_stream() -> Request:
    return Request("GET", "/api/event")


def create_session(cwd: str, *, model: str = "", effort: str = "",
                   session_id: str = "") -> Request:
    body: dict = {"location": {"directory": cwd}}
    ref = model_ref(model, effort)
    if ref is not None:
        body["model"] = ref
    if is_session_id(session_id):
        body["id"] = session_id
    return Request("POST", "/api/session", body)


def list_models() -> Request:
    """``GET /api/model``: every model the server can run, each with its
    ``providerID``, ``id`` and ``name`` (providers recording, 2.3.2)."""
    return Request("GET", "/api/model")


def model_choices(body: Any) -> list[dict]:
    """``session.options`` choices from a ``GET /api/model`` answer: the
    ``provider/model`` ref a session takes, and the server's name."""
    data = body.get("data") if isinstance(body, dict) else None
    out = []
    for m in data if isinstance(data, list) else ():
        if isinstance(m, dict) and text_of(m.get("providerID")) \
                and text_of(m.get("id")):
            out.append(opts.choice(f"{m['providerID']}/{m['id']}",
                                   m.get("name")))
    return out


def get_session(session_id: str) -> Request:
    return Request("GET", _session(session_id))


def fork_session(session_id: str) -> Request:
    return Request("POST", _session(session_id, "/fork"), {})


def prompt(session_id: str, text: str, *, attachments: Iterable[str] = (),
           steer: bool = False) -> Request:
    """One user message. ``delivery`` ``steer`` puts it into the turn in
    flight at the next step; with no turn running either delivery
    starts one. Attached files go as ``file://`` URIs."""
    body: dict = {"text": text}
    files = [{"uri": "file://" + urllib.parse.quote(str(path)),
              "name": str(path).rsplit("/", 1)[-1]} for path in attachments]
    if files:
        body["files"] = files
    if steer:
        body["delivery"] = "steer"
    return Request("POST", _session(session_id, "/prompt"), body)


def interrupt(session_id: str) -> Request:
    return Request("POST", _session(session_id, "/interrupt"))


def permission_reply(session_id: str, request_id: str, decision: str,
                     message: str = "") -> Request:
    """``decision`` is one of ``DECISIONS``' values."""
    body: dict = {"decision": decision}
    if message:
        body["message"] = message
    return Request("POST", _session(
        session_id, f"/permission/{seg(request_id)}/reply"), body)


def form_reply(session_id: str, form_id: str, answer: dict) -> Request:
    return Request("POST", _session(session_id, f"/form/{seg(form_id)}/reply"),
                   {"answer": answer})


def form_cancel(session_id: str, form_id: str) -> Request:
    return Request("DELETE", _session(session_id, f"/form/{seg(form_id)}"))


def select_model(session_id: str, ref: dict) -> Request:
    return Request("POST", _session(session_id, "/model"), {"model": ref})


def select_agent(session_id: str, agent: str) -> Request:
    return Request("POST", _session(session_id, "/agent"), {"agent": agent})


def revert_stage(session_id: str, message_id: str) -> Request:
    """Stage a revert to before ``message_id``. ``files`` false: the
    conversation only, files are the keeper's checkpoints."""
    return Request("POST", _session(session_id, "/revert/stage"),
                   {"messageID": message_id, "files": False})


def revert_commit(session_id: str) -> Request:
    return Request("POST", _session(session_id, "/revert/commit"))


def revert_clear(session_id: str) -> Request:
    """Drop a staged revert that was not committed."""
    return Request("DELETE", _session(session_id, "/revert"))


def error_text(body: Any) -> str:
    """What an error answer says (``{"_tag", "message"}``)."""
    if isinstance(body, dict):
        return text_of(body.get("message")) or text_of(body.get("_tag"))
    return ""


def error_tag(body: Any) -> str:
    return text_of(body.get("_tag")) if isinstance(body, dict) else ""


# ---------------------------------------------------------------------
# The event stream


class SseParser:
    """Server-sent events, line by line: ``feed`` returns the JSON
    object of an event when a blank line ends it, else None."""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: bytes | str) -> dict | None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            self._data.append(line[5:].lstrip())
            return None
        if line:
            return None  # ``event:``, ``id:`` and comments
        data, self._data = "\n".join(self._data), []
        if not data:
            return None
        try:
            event = json.loads(data)
        except ValueError:
            return None
        return event if isinstance(event, dict) else None


# ---------------------------------------------------------------------
# What the server's events mean


_ALLOW = {"id": "allow", "label": "Allow", "kind": "allow"}
_DENY = {"id": "deny", "label": "Deny", "kind": "deny"}
_TURN_STATUS = {"session.execution.succeeded": "completed",
                "session.execution.failed": "failed",
                "session.execution.interrupted": "interrupted"}
_EDIT_KIND = {"added": "add", "deleted": "delete", "modified": "modify"}
_COMMAND_ACTIONS = frozenset({"shell", "bash"})
# Tool calls remembered past their turn's end, at most.
_TOOLS_MAX = 200


def _content_text(content: Any) -> str:
    parts = [text_of(block.get("text")) for block in content or ()
             if isinstance(block, dict)]
    return "\n".join(p for p in parts if p)


def file_edits(metadata: Any) -> list | None:
    """``file_edits`` from a tool result's or a permission's
    ``metadata.files`` (``[{file, patch, status}]``)."""
    files = metadata.get("files") if isinstance(metadata, dict) else None
    if not isinstance(files, list):
        return None
    edits = []
    for entry in files:
        if not isinstance(entry, dict) or not text_of(entry.get("file")):
            continue
        edit: dict[str, Any] = {
            "path": entry["file"],
            "kind": _EDIT_KIND.get(text_of(entry.get("status")), "modify")}
        patch = text_of(entry.get("patch"))
        at = patch.find("@@")
        if at >= 0:
            edit["diff"] = patch[at:]
        edits.append(edit)
    return redact(edits) or None


@dataclass
class _Pending:
    """A permission or a form waiting on us."""

    kind: str  # "permission" or "form"
    request_id: str
    key: str = ""  # a form's one field
    options: list = field(default_factory=list)


@dataclass
class _Expected:
    """A prompt posted, its answer not yet read."""

    steer: bool
    text: str
    attachments: tuple = ()
    command_id: str = ""
    device: str = ""


# ---------------------------------------------------------------------
# The session state machine


class ServeState(SessionCore):
    """One opencode session's protocol state, from the adapter's side.

    The adapter calls ``start_events`` after the handshake,
    ``expect_turn`` or ``expect_steer`` before it posts a prompt and
    ``accepted`` (or ``forget``) with the answer, ``answer`` per
    ``respond``, ``interrupt_sent`` per ``interrupt``, ``feed`` per
    event of the stream, and ``exited`` or ``closed`` once at the end.

    Event by event (``feed``), for this session only:

    ===========================================  =========================
    Server event                                 v2 events
    ===========================================  =========================
    ``session.execution.started``                ``turn.started`` when no
                                                 turn is open
    ``session.inbox.delivered``                  a steer landing: open
                                                 streams completed, the
                                                 user's
                                                 ``message.completed``
    ``session.text.delta`` / ``.ended``          ``message.delta`` /
                                                 ``message.completed``
    ``session.reasoning.delta`` / ``.ended``     ``message.delta`` /
                                                 ``thinking.completed``
    ``session.tool.called``                      ``tool.started``
    ``session.tool.success`` / ``.failed``       ``tool.completed``
    ``permission.asked``                         ``approval.requested``,
                                                 ``session.state`` waiting
    ``permission.replied``                       ``approval.resolved``
                                                 (``harness``) if still
                                                 open
    ``form.created`` (one field)                 ``input.requested``
    ``session.usage.updated``                    ``usage.updated``
    ``session.compaction.ended``                 ``context.compacted``
    ``session.step.failed`` (not aborted)        ``error``
    ``session.execution.succeeded`` /            open streams closed,
    ``.failed`` / ``.interrupted``               requests retired,
                                                 ``turn.ended``,
                                                 ``session.state`` idle
    anything else                                nothing (``unknown``)
    ===========================================  =========================
    """

    def __init__(self, session: str, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.model: dict = {}
        self.agent = ""
        # The server's models, as ``session.options`` choices (G32).
        self.model_choices: list[dict] = []
        # The model refs and agents this adapter posted that the server
        # has not reported back yet, oldest first. The server reports
        # each post on the event stream, which can lag the post's own
        # answer: a report equal to a pending post is that post's echo
        # and changes nothing (the newest post is shown already); any
        # other report is the server's own change, shown at once.
        self._posted_models: list[dict] = []
        self._posted_agents: list[str] = []
        # Bumped by every post, every report, a step's start that names a
        # value, and every re-read started:
        # a re-read's answer is used only if nothing came after its ask.
        self._generation = 0
        # our turn id -> the id of its user message, for a revert
        self.turn_messages: dict[str, str] = {}
        self._expected: dict[Any, _Expected] = {}
        # token of an accepted prompt -> our turn id
        self.accepted_turns: dict[Any, str] = {}
        # steers the server took, by inbox id, not yet delivered; and
        # the tokens of steers delivered before their answer was read
        self._steers: dict[str, _Expected] = {}
        self._early: set = set()
        # call id -> (tool name, the turn it started in)
        self._tools: dict[str, tuple[str, str]] = {}
        self._names: dict[str, str] = {}
        self._step: dict = {}
        self._errored = False

    # -- calls from the adapter ------------------------------------------

    @property
    def busy(self) -> bool:
        """A turn is open or one is being started."""
        return self.turn is not None or any(
            not e.steer for e in self._expected.values())

    def expect_turn(self, token: Any, text: str, *,
                    attachments: Iterable[str] = (), command_id: str = "",
                    device: str = "") -> None:
        self._expected[token] = _Expected(
            False, text, tuple(attachments), command_id, device)

    def expect_steer(self, token: Any, text: str, *,
                     attachments: Iterable[str] = (), command_id: str = "",
                     device: str = "") -> None:
        self._expected[token] = _Expected(
            True, text, tuple(attachments), command_id, device)

    def forget(self, token: Any) -> None:
        """The prompt was refused, or never answered."""
        self._expected.pop(token, None)

    def accepted(self, token: Any, message: Any) -> list:
        """The server took the prompt posted under ``token``; ``message``
        is the answer's ``data`` (its ``id`` names the message).
        ``accepted_turns`` gets the token either way: with the turn the
        message is in, or "" for a steer that waits in the inbox with
        no turn open."""
        message_id = text_of(message.get("id")) \
            if isinstance(message, dict) else ""
        if token in self._early:
            # A steer its delivery already said.
            self._early.discard(token)
            return []
        expected = self._expected.pop(token, None)
        if expected is None:
            # Its events overtook the answer and opened the turn
            # already: only the message's id was still missing.
            turn_id = self.accepted_turns.get(token)
            if turn_id and message_id:
                self.turn_messages.setdefault(turn_id, message_id)
            return []
        out: list = []
        if expected.steer and message_id:
            # In the inbox. It is said when the server delivers it: to
            # this turn at its next step, or to the execution the
            # server starts for it later (``_ensure_turn``). Never by
            # opening a turn here: after an interrupt the server keeps
            # the message until the next prompt, and a turn opened for
            # it would not end.
            self._steers[message_id] = expected
            self.accepted_turns[token] = self.tid() or ""
            return out
        if expected.steer and self.turn is None:
            # No id to know its delivery by, and no turn to put it in.
            self.accepted_turns[token] = ""
            return out
        if self.turn is None:
            self.open_turn(out, by=expected.device, text=expected.text,
                           command_id=expected.command_id)
        elif not expected.steer and not self.turn.command_id:
            self.turn.command_id = expected.command_id
        self._say(out, expected, message_id, cut=expected.steer)
        self.accepted_turns[token] = self.turn.turn_id
        return out

    def _say(self, out: list, expected: _Expected, message_id: str,
             cut: bool = False) -> None:
        """The user's ``message.completed`` for a prompt that is in the
        turn now; a steer first completes whatever was streaming."""
        assert self.turn is not None
        if cut:
            self.close_streams(out)
        if self.turn.user_msgs == 0 and message_id:
            self.turn_messages[self.turn.turn_id] = message_id
        self.out(out, self.user_message(
            expected.text, attachments=expected.attachments,
            command_id=expected.command_id, device=expected.device))

    def turn_cleared(self) -> None:
        while len(self._tools) > _TOOLS_MAX:
            self._tools.pop(next(iter(self._tools)))
        self._names.clear()
        self._errored = False

    def answer(self, session_id: str, request_id: str,
               answer: Answer) -> tuple[Request, list]:
        """The request that answers an open permission or form, and the
        ``approval.resolved`` it closes. Raises UnknownRequest."""
        pending: _Pending = self.take(request_id)
        if pending.kind == "form":
            value = answer.text
            if not value and answer.option_id:
                value = next((o["value"] for o in pending.options
                              if o["id"] == answer.option_id),
                             answer.option_id)
            return form_reply(session_id, pending.request_id,
                              {pending.key: value}), \
                self.answered(request_id, answer)
        decision = answer.decision or (answer.option_id if answer.option_id
                                       in DECISIONS else "")
        if not decision:
            # A bare text answer is feedback: the call does not run.
            decision = "deny"
        if decision == "always":
            # "Always" would save a rule for the whole project in
            # opencode's own store; a tap here never does that.
            decision = "allow"
        request = permission_reply(
            session_id, pending.request_id, DECISIONS[decision],
            answer.text if decision == "deny" else "")
        return request, self.answered(request_id, answer, decision)

    # -- events from the server ------------------------------------------

    def feed(self, event: Any) -> Fed:
        fed = Fed()
        if self.done or not isinstance(event, dict):
            return fed
        etype = event.get("type")
        data = event.get("data")
        if not isinstance(etype, str) or not isinstance(data, dict):
            self.note_unknown("?")
            return fed
        session = data.get("sessionID")
        if isinstance(session, str) and self.session_id \
                and session != self.session_id:
            self.note_unknown("other-session")
            return fed
        handler = _EVENTS.get(etype)
        if handler is None:
            self.note_unknown(etype)
            return fed
        handler(self, data, fed)
        self.sync_options(fed.events)
        return fed

    def options(self) -> dict:
        """The model (``provider/model``) with the server's list
        (``GET /api/model``), its variant as the effort (value only: the
        list's variants are not kept), and the ``plan`` agent as plan
        mode. Each change is posted at once, and the step being run
        finishes as it started (``reply_finishes``): model_mid ran its
        one step on the old model and the next turn on the new one;
        plan_mode_mid's next step, in the same execution, ran as
        ``plan``. The effort is the model ref's variant, the same
        request as a model change."""
        return {
            "model": opts.string_option(model_name(self.model),
                                        opts.REPLY_FINISHES,
                                        choices=self.model_choices or None),
            "effort": opts.string_option(
                text_of(self.model.get("variant")), opts.REPLY_FINISHES),
            "plan_mode": opts.flag_option(self.agent == PLAN_AGENT,
                                          opts.REPLY_FINISHES),
        }

    def model_selected(self, ref: Any) -> list:
        """The server took ``POST .../model`` ``ref``."""
        if isinstance(ref, dict):
            self.model = dict(ref)
            self._posted_models.append(dict(ref))
            self._generation += 1
        return self.options_events()

    def agent_selected(self, agent: Any) -> list:
        """The server took ``POST .../agent`` ``agent``."""
        if text_of(agent):
            self.agent = agent
            self._posted_agents.append(agent)
            self._generation += 1
        return self.options_events()

    def events_lost(self) -> int:
        """An event the reader had to drop may have been a post's echo:
        no report is taken for an echo any more, so every later one is
        the server's word. Returns the generation to hand the session
        re-read (any earlier re-read is overtaken by this one)."""
        self._posted_models.clear()
        self._posted_agents.clear()
        self._generation += 1
        return self._generation

    def session_read(self, session: Any, generation: int) -> list:
        """``GET .../session/<id>`` after lost events: the server's
        current model and agent. An answer overtaken by anything newer
        (a post, a server report, a later re-read) is not used: what
        came after it said more."""
        if generation != self._generation or not isinstance(session, dict):
            return []
        if isinstance(session.get("model"), dict):
            self.model = dict(session["model"])
        if text_of(session.get("agent")):
            self.agent = session["agent"]
        return self.options_events()

    def _ensure_turn(self, out: list) -> None:
        """A turn the server runs whose prompt's answer is still on its
        way, or one we did not start."""
        if self.turn is not None:
            return
        for token, expected in list(self._expected.items()):
            if not expected.steer:
                del self._expected[token]
                self.open_turn(out, by=expected.device, text=expected.text,
                               command_id=expected.command_id)
                self._say(out, expected, "")
                self.accepted_turns[token] = self.turn.turn_id
                return
        for steer in self._steers.values():
            # A steer the last turn never reached: this turn is its
            # answer. ``session.inbox.delivered`` says the message.
            self.open_turn(out, by=steer.device, text=steer.text,
                           command_id=steer.command_id)
            return
        self.open_turn(out)

    def _on_started(self, data: dict, fed: Fed) -> None:
        self._ensure_turn(fed.events)

    def _on_delivered(self, data: dict, fed: Fed) -> None:
        inbox = text_of(data.get("inboxID"))
        steer = self._steers.pop(inbox, None)
        if steer is None:
            if not inbox or inbox in self.turn_messages.values():
                return  # a turn's own first message
            # A steer delivered before its prompt's answer was read:
            # the oldest one posted. (A turn's first message whose
            # answer is still on its way finds no steer waiting.)
            token = next((t for t, e in self._expected.items() if e.steer),
                         None)
            if token is None:
                return
            steer = self._expected.pop(token)
            self._early.add(token)
        else:
            token = None
        if self.turn is None:
            self.open_turn(fed.events, by=steer.device, text=steer.text,
                           command_id=steer.command_id)
        if token is not None:
            self.accepted_turns[token] = self.turn.turn_id
        self._say(fed.events, steer, inbox, cut=True)

    def _on_ended(self, etype: str, data: dict, fed: Fed) -> None:
        turn = self.turn
        if turn is None:
            return
        status = _TURN_STATUS[etype]
        if status == "failed":
            failure = data.get("error")
            self._fail(fed.events, failure if isinstance(failure, dict)
                       else {}, "the turn failed")
        # A steer still in the inbox stays there: the server delivers it
        # to the execution it starts next (``_ensure_turn``).
        self.finish_turn(fed.events, status)
        # By now the server has reported every post it took: an echo it
        # never sent is not waited for any longer.
        self._posted_models.clear()
        self._posted_agents.clear()

    def _fail(self, out: list, failure: dict, fallback: str) -> None:
        turn = self.turn
        if turn is None or self._errored:
            return
        self._errored = True
        turn.error = True
        turn.reason = redact(text_of(failure.get("message"))
                             or fallback)[:300]
        self.out(out, ev.error(
            self.key, redact(text_of(failure.get("type")) or "error"),
            turn.reason, turn_id=turn.turn_id, ts=self.now()))

    def _on_step_started(self, data: dict, fed: Fed) -> None:
        # A step says what it runs with, which can be the value from
        # before a change posted while the execution was under way (the
        # step finishes as it started). It names the session's model or
        # agent only while we know none; the session's setting comes from
        # our posts and the server's ``*.selected`` reports.
        if not self.model and isinstance(data.get("model"), dict):
            self.model = dict(data["model"])
            self._generation += 1
        if not self.agent and text_of(data.get("agent")):
            self.agent = data["agent"]
            self._generation += 1

    def _on_step_ended(self, data: dict, fed: Fed) -> None:
        if isinstance(data.get("tokens"), dict):
            self._step = data["tokens"]

    def _on_step_failed(self, data: dict, fed: Fed) -> None:
        if isinstance(data.get("tokens"), dict):
            # A step that failed (or was cut) still used tokens.
            self._step = data["tokens"]
        failure = data.get("error")
        if isinstance(failure, dict) and failure.get("type") != "aborted":
            self._fail(fed.events, failure, "the step failed")

    @staticmethod
    def _stream_id(data: dict, kind: str) -> str:
        message = text_of(data.get("assistantMessageID"))
        ordinal = data.get("ordinal")
        if not message:
            return ""
        return f"{message}:{'r' if kind == 'thinking' else ''}{ordinal}"

    def _on_text_delta(self, data: dict, fed: Fed) -> None:
        if self.turn is not None:
            self.delta(fed.events, self._stream_id(data, "assistant"),
                       "assistant", text_of(data.get("delta")))

    def _on_text_ended(self, data: dict, fed: Fed) -> None:
        sid = self._stream_id(data, "assistant")
        text = text_of(data.get("text"))
        if self.turn is None or not sid:
            return
        if text or self._streamed(sid):
            # An end that names no text ends what was streamed.
            self.complete(fed.events, sid, "assistant",
                          text or self.streams[sid].raw)

    def _on_reasoning_delta(self, data: dict, fed: Fed) -> None:
        if self.turn is not None:
            self.delta(fed.events, self._stream_id(data, "thinking"),
                       "thinking", text_of(data.get("delta")))

    def _on_reasoning_ended(self, data: dict, fed: Fed) -> None:
        sid = self._stream_id(data, "thinking")
        text = text_of(data.get("text"))
        if self.turn is None or not sid:
            return
        if text or self._streamed(sid):
            # (Hidden reasoning ends with no text: nothing to show.)
            self.complete(fed.events, sid, "thinking",
                          text or self.streams[sid].raw)

    def _streamed(self, sid: str) -> bool:
        block = self.streams.get(sid)
        return block is not None and bool(block.raw)

    def _on_tool_named(self, data: dict, fed: Fed) -> None:
        if text_of(data.get("id")) and text_of(data.get("name")):
            self._names[data["id"]] = data["name"]

    def _on_tool_called(self, data: dict, fed: Fed) -> None:
        call_id = text_of(data.get("id"))
        if not call_id or call_id in self._tools:
            return
        self._ensure_turn(fed.events)
        name = redact(self._names.pop(call_id, "")
                      or text_of(data.get("name")) or "tool")
        tool_input = data.get("input")
        self._tools[call_id] = (name, self.tid() or "")
        self.out(fed.events, ev.tool_started(
            self.key, call_id, name, summary=tool_summary(name, tool_input),
            input=jsonable(redact(tool_input)), turn_id=self.tid(),
            ts=self.now()))

    def _tool_done(self, data: dict, fed: Fed, status: str, output: str,
                   edits: list | None = None) -> None:
        call_id = text_of(data.get("id"))
        if call_id not in self._tools:
            return
        name, turn_id = self._tools.pop(call_id)
        # A call that outlived its turn completes outside it.
        same_turn = bool(turn_id) and turn_id == self.tid()
        self.out(fed.events, ev.tool_completed(
            self.key, call_id, status, name=name,
            output=clip(redact(output)), file_edits=edits,
            turn_id=turn_id if same_turn else None, ts=self.now()))

    def _on_tool_success(self, data: dict, fed: Fed) -> None:
        self._tool_done(data, fed, "ok", _content_text(data.get("content")),
                        file_edits(data.get("metadata")))

    def _on_tool_failed(self, data: dict, fed: Fed) -> None:
        failure = data.get("error")
        failure = failure if isinstance(failure, dict) else {}
        self._tool_done(
            data, fed,
            "cancelled" if failure.get("type") == "aborted" else "error",
            text_of(failure.get("message")))

    def _on_permission_asked(self, data: dict, fed: Fed) -> None:
        request_id = text_of(data.get("id"))
        if not request_id or request_id in self.pending:
            return
        self._ensure_turn(fed.events)
        action = text_of(data.get("action")) or "tool"
        resources = [r for r in data.get("resources") or ()
                     if isinstance(r, str)]
        prompt_text = "\n".join(resources[:8]) or action
        if action not in _COMMAND_ACTIONS:
            prompt_text = f"{action}: {prompt_text}"
        note = text_of(data.get("message"))
        if note:
            prompt_text += f"\n\n{note}"
        source = data.get("source")
        call_id = text_of(source.get("id")) if isinstance(source, dict) \
            else ""
        self.ask(fed.events, request_id,
                 "command" if action in _COMMAND_ACTIONS else "tool",
                 prompt_text, _Pending("permission", request_id),
                 options=[_ALLOW, _DENY], call_id=call_id)

    def _on_permission_replied(self, data: dict, fed: Fed) -> None:
        self.withdrawn(fed.events, text_of(data.get("requestID")))

    def _on_form_created(self, data: dict, fed: Fed) -> None:
        form = data.get("form")
        if not isinstance(form, dict) or (
                self.session_id and form.get("sessionID") != self.session_id):
            return
        form_id = text_of(form.get("id"))
        fields = [f for f in form.get("fields") or () if isinstance(f, dict)
                  and text_of(f.get("key")) and not f.get("hidden")]
        if not form_id or form_id in self.pending:
            return
        if len(fields) != 1 or fields[0].get("type") != "string":
            # No card for anything but one text question: let the form
            # go, so the agent is not left waiting on us.
            self.note_unknown("form.created")
            fed.replies.append(form_cancel(self.session_id, form_id))
            return
        self._ensure_turn(fed.events)
        one = fields[0]
        # The model wrote the choices: devices and the log get an opaque
        # id and the redacted label; the value stays here for the answer.
        values = [o for o in one.get("options") or ()
                  if isinstance(o, dict) and text_of(o.get("value"))]
        shown = [{"id": f"o{n}", "label": redact(
            text_of(o.get("label")) or o["value"])}
            for n, o in enumerate(values, 1)]
        question = text_of(one.get("title")) or text_of(form.get("title")) \
            or text_of(one.get("description"))
        self.ask_input(
            fed.events, form_id, question,
            _Pending("form", form_id, key=one["key"],
                     options=[{"id": f"o{n}", "value": o["value"]}
                              for n, o in enumerate(values, 1)]),
            options=shown)

    def _on_form_settled(self, data: dict, fed: Fed) -> None:
        self.withdrawn(fed.events, text_of(data.get("id")))

    def _on_usage(self, data: dict, fed: Fed) -> None:
        """The session's running cost, with the last model call's tokens
        once a step of this start has ended (the event's own ``tokens``
        are sums over the session, another thing)."""
        tokens = self._step
        cache = tokens.get("cache") if isinstance(tokens.get("cache"),
                                                  dict) else {}
        counts: dict[str, Any] = {
            "input_tokens": count(tokens.get("input")),
            "output_tokens": count(tokens.get("output")),
            "cache_read_tokens": count(cache.get("read")),
            "cache_write_tokens": count(cache.get("write"))}
        counts = {k: v for k, v in counts.items() if v is not None}
        cost = data.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) \
                and cost >= 0 and cost == cost and cost != float("inf"):
            counts["cost_usd"] = float(cost)
        if counts:
            self.out(fed.events, ev.usage_updated(
                self.key, turn_id=self.tid(), ts=self.now(), **counts))

    def _on_compacted(self, data: dict, fed: Fed) -> None:
        self.out(fed.events, ev.context_compacted(
            self.key, turn_id=self.tid(), ts=self.now()))

    def _on_model(self, data: dict, fed: Fed) -> None:
        model = data.get("model")
        if not isinstance(model, dict):
            return
        self._generation += 1
        for i, posted in enumerate(self._posted_models):
            if _same_ref(model, posted):
                del self._posted_models[i]  # that post's echo
                return
        self._posted_models.clear()  # the server's own change
        self.model = dict(model)

    def _on_agent(self, data: dict, fed: Fed) -> None:
        agent = text_of(data.get("agent"))
        if not agent:
            return
        self._generation += 1
        if agent in self._posted_agents:
            self._posted_agents.remove(agent)  # that post's echo
            return
        self._posted_agents.clear()  # the server's own change
        self.agent = agent


def _same_ref(a: dict, b: dict) -> bool:
    """Whether two model refs name the same model and variant."""
    def key(ref: dict) -> tuple:
        return (text_of(ref.get("providerID")),
                text_of(ref.get("id") or ref.get("modelID")),
                text_of(ref.get("variant")))
    return key(a) == key(b)


_EVENTS = {
    "session.execution.started": ServeState._on_started,
    "session.execution.succeeded": lambda s, d, f: s._on_ended(
        "session.execution.succeeded", d, f),
    "session.execution.failed": lambda s, d, f: s._on_ended(
        "session.execution.failed", d, f),
    "session.execution.interrupted": lambda s, d, f: s._on_ended(
        "session.execution.interrupted", d, f),
    "session.inbox.delivered": ServeState._on_delivered,
    "session.step.started": ServeState._on_step_started,
    "session.step.ended": ServeState._on_step_ended,
    "session.step.failed": ServeState._on_step_failed,
    "session.text.delta": ServeState._on_text_delta,
    "session.text.ended": ServeState._on_text_ended,
    "session.reasoning.delta": ServeState._on_reasoning_delta,
    "session.reasoning.ended": ServeState._on_reasoning_ended,
    "session.tool.input.started": ServeState._on_tool_named,
    "session.tool.called": ServeState._on_tool_called,
    "session.tool.success": ServeState._on_tool_success,
    "session.tool.failed": ServeState._on_tool_failed,
    "permission.asked": ServeState._on_permission_asked,
    "permission.replied": ServeState._on_permission_replied,
    "form.created": ServeState._on_form_created,
    "form.replied": ServeState._on_form_settled,
    "form.cancelled": ServeState._on_form_settled,
    "session.usage.updated": ServeState._on_usage,
    "session.compaction.ended": ServeState._on_compacted,
    "session.model.selected": ServeState._on_model,
    "session.agent.selected": ServeState._on_agent,
}

# Events of this module's own, read by ``feed``: the recorder keeps
# these whole and cuts every other event down to its type.
READ_EVENTS = frozenset(_EVENTS)


# ---------------------------------------------------------------------
# Replaying a recorded fixture


_PROMPT_RE = re.compile(r"/api/session/[^/]+/prompt$")
_REPLY_RE = re.compile(r"/api/session/[^/]+/permission/([^/]+)/reply$")
_INTERRUPT_RE = re.compile(r"/api/session/[^/]+/interrupt$")
_SESSION_PATHS = re.compile(r"/api/session(/[^/]+(/fork)?)?$")


def _answer_from_reply(body: Any) -> Answer:
    decision = body.get("decision") if isinstance(body, dict) else None
    for ours, theirs in DECISIONS.items():
        if decision == theirs:
            return Answer(decision=ours, text=text_of(body.get("message")))
    return Answer(decision="deny")


def replay(lines: list[dict], *, session: str = FIXTURE_SESSION) -> list:
    """The v2 events a recorded fixture comes to, through the same
    :class:`ServeState` calls the adapter makes.

    A fixture line is one of ``{"dir": "out", "http": {"n", "method",
    "path", "body"}}`` (a request we made), ``{"dir": "in", "http":
    {"n", "status", "body"}}`` (its answer), ``{"dir": "in", "sse":
    {...}}`` (one event of the stream) or ``{"exit": code}``, each with
    the process it belongs to and the seconds since that process
    started. Timestamps are those on a fixed epoch."""
    header = lines[0] if lines and "fixture" in lines[0] else {}
    now = [0.0]

    def clock() -> float:
        return round(FIXTURE_T0 + now[0], 3)

    out: list = []
    states: dict[int, ServeState] = {}
    requests: dict[tuple, dict] = {}
    for line in lines[1:] if header else lines:
        proc = line.get("proc", 0)
        if isinstance(line.get("t"), (int, float)):
            now[0] = float(line["t"])
        state = states.get(proc)
        if state is None:
            state = states[proc] = ServeState(
                session, clock=clock, run_id=f"{proc + 1:08x}")
        if "exit" in line:
            out.extend(state.closed())
            continue
        if isinstance(line.get("sse"), dict):
            out.extend(state.feed(line["sse"]).events)
            continue
        http = line.get("http")
        if not isinstance(http, dict):
            continue
        key = (proc, http.get("n"))
        if line.get("dir") == "out":
            requests[key] = http
            out.extend(_replay_request(state, http))
        else:
            out.extend(_replay_answer(state, requests.pop(key, {}), http))
    return out


def _replay_request(state: ServeState, http: dict) -> list:
    path = text_of(http.get("path"))
    body = http.get("body") if isinstance(http.get("body"), dict) else {}
    if _PROMPT_RE.search(path) and state.started:
        text = text_of(body.get("text"))
        if body.get("delivery") == "steer" and state.turn is not None:
            state.expect_steer(http.get("n"), text)
        else:
            state.expect_turn(http.get("n"), text)
    elif _INTERRUPT_RE.search(path):
        return state.interrupt_sent()
    else:
        reply = _REPLY_RE.search(path)
        request_id = urllib.parse.unquote(reply.group(1)) if reply else ""
        if request_id in state.pending:
            return state.answer(state.session_id, request_id,
                                _answer_from_reply(body))[1]
    return []


def _replay_answer(state: ServeState, request: dict, http: dict) -> list:
    path = text_of(request.get("path"))
    ok = http.get("status") in (200, 204)
    body = http.get("body") if isinstance(http.get("body"), dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if not state.started:
        # The session the process is about: created, fetched or forked.
        if ok and request.get("method") in ("POST", "GET") \
                and _SESSION_PATHS.search(path) and is_session_id(
                    data.get("id")):
            if isinstance(data.get("model"), dict):
                state.model = dict(data["model"])
            if text_of(data.get("agent")):
                state.agent = data["agent"]
            return state.start_events(ALL_CAPS.narrowed(PROVEN), data["id"])
        return []
    sent = request.get("body") if isinstance(request.get("body"), dict) \
        else {}
    if ok and path == "/api/model":
        state.model_choices = model_choices(body)
        return state.options_events()
    if ok and path.endswith("/model"):
        return state.model_selected(sent.get("model"))
    if ok and path.endswith("/agent"):
        return state.agent_selected(sent.get("agent"))
    if _PROMPT_RE.search(path):
        if ok:
            return state.accepted(request.get("n"), data)
        state.forget(request.get("n"))
        return []
    return []


# ---------------------------------------------------------------------
# The pre-keeper channel (``inject.OpencodeServeInjector``)
#
# opencode 1.x's serve: no ``/api`` prefix and no auth. The channel
# posts one prompt at a time into a session started in a terminal and
# takes only approvals and live text off ``GET /event``; the transcript
# is still read from opencode's store. opencode 2.x answers these paths
# with 405, so this channel only works against a 1.x CLI. It goes away
# with the tmux paths (G22, G24).

LEGACY_DECISIONS = {"allow": "once", "always": "always", "deny": "reject"}
LEGACY_SERVE_ARGS = ("--hostname", "127.0.0.1", "--port")


def legacy_prompt(session_id: str, text: str, *, model: dict | None = None,
                  variant: str = "") -> Request:
    body: dict = {"parts": [{"type": "text", "text": text}]}
    if model:
        body["model"] = model
    if variant:
        body["variant"] = variant
    return Request("POST", f"/session/{seg(session_id)}/prompt_async", body)


def legacy_abort(session_id: str) -> Request:
    return Request("POST", f"/session/{seg(session_id)}/abort", {})


def legacy_permission_reply(session_id: str, request_id: str,
                            response: str) -> Request:
    return Request(
        "POST", f"/session/{seg(session_id)}/permissions/{seg(request_id)}",
        {"response": response})


def legacy_permission_reply_old(request_id: str, response: str) -> Request:
    """The reply route of older 1.x builds."""
    return Request("POST", f"/permission/{seg(request_id)}/reply",
                   {"reply": response})


def legacy_event(blob: bytes) -> tuple[str, dict] | None:
    """One ``GET /event`` payload as ``(type, properties)``."""
    try:
        event = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    props = event.get("properties") if isinstance(
        event.get("properties"), dict) else event.get("payload")
    if not isinstance(props, dict):
        return None
    return str(event.get("type") or ""), props


def legacy_is_live(etype: str) -> bool:
    """Whether an event is about a reply being streamed."""
    return etype.startswith("message.part.") or etype == "session.idle"


def legacy_permission(etype: str, props: dict,
                      known: dict) -> tuple[str, str, str, str] | None:
    """A permission event as ``(what, permission id, session id,
    prompt)``: ``what`` is ``resolved`` or ``asked``; None for any
    other event. ``known`` maps permission ids to session ids already
    seen. The names have drifted between 1.x builds (asked, updated,
    replied), so anything that is not a reply counts as an ask."""
    if "permission" not in etype.lower():
        return None
    perm_id = str(props.get("id") or props.get("permissionID") or "")
    session_id = str(props.get("sessionID") or props.get("sessionId")
                     or known.get(perm_id) or "")
    if any(w in etype.lower() for w in ("replied", "resolved")):
        return "resolved", perm_id, session_id, ""
    text = str(props.get("title") or props.get("permission")
               or props.get("message") or "permission requested")
    patterns = props.get("patterns") or props.get("metadata") or {}
    if isinstance(patterns, (list, tuple)):
        text += "\n\n" + "\n".join(f"`{p}`" for p in patterns[:8])
    return "asked", perm_id, session_id, text
