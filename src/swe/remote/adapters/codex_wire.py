"""The Codex app-server wire format: the only module that knows it.

``codex app-server`` speaks JSON-RPC 2.0, one JSON object per line on
stdio, in both directions. Three kinds of line:

- our requests (``{"jsonrpc", "id", "method", "params"}``) and the
  server's answers to them (``{"id", "result"}`` or ``{"id", "error"}``);
- the server's notifications (``{"method", "params"}``, no ``id``):
  ``turn/started``, ``item/started``, ``item/agentMessage/delta``,
  ``item/completed``, ``thread/tokenUsage/updated``,
  ``turn/completed``...;
- the server's own requests (``{"id", "method", "params"}``), which wait
  for our answer under their ``id``: the approvals
  (``item/commandExecution/requestApproval``,
  ``item/fileChange/requestApproval``,
  ``item/permissions/requestApproval``) and
  ``item/tool/requestUserInput``.

Everything here is pure: builders for the lines we write, ``decode``
for the lines we read, and :class:`ThreadState`, which turns the
server's lines into v2 events (``events.py``). No process, no thread,
no clock of its own, so a recorded fixture replays through the same
code the live adapter runs (:func:`replay`). ``codex_appserver.py``
owns the process; ``inject.CodexAppServerInjector`` (the pre-keeper
channel) builds its lines here too.

The shapes were read off ``codex app-server generate-json-schema`` and
recordings of the real CLI
(tests/fixtures/protocols/codex_appserver/); see
docs/specs/G16-more-adapters.md for what each fixture proves.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from swe.remote.adapters import events as ev
from swe.remote.adapters.base import Answer, Caps
from swe.remote.adapters.core import (
    ALL_CAPS, FIXTURE_SESSION, FIXTURE_T0, Ack, Fed, SessionCore, clip,
    count, jsonable, redact, text_of, tool_summary,
)
from swe.remote.adapters.claude_wire import parse_version, version_ok

__all__ = [
    "ThreadState", "replay", "decode", "encode", "PROVEN", "MIN_VERSION",
    "RECORDED_VERSION", "DEFAULT_ARGV", "DECISIONS", "parse_version",
    "version_ok",
]

# The command that starts the endpoint; harness.json's
# ``capabilities.session.argv`` replaces it. Nothing is appended: the
# session is chosen on the wire (``thread/start``), not on the argv.
DEFAULT_ARGV = ("codex", "app-server")

# The CLI version the fixtures were recorded against, and the oldest one
# ``start`` accepts when harness.json names no ``min_version``: nothing
# older was ever run against this module.
RECORDED_VERSION = "0.170.0"
MIN_VERSION = "0.170.0"

# The caps this module can deliver, each proven by a recording.
# ``plan_mode`` (``collaborationMode``, experimental API only) and
# ``stop_task`` (no request stops one sub-agent) stay off whatever
# harness.json says.
PROVEN = Caps(steer=True, rollback=True, fork=True,
              options=frozenset({"model", "effort", "permission_mode"}))

# ``approvalPolicy`` values: what ``permission_mode`` means on Codex.
APPROVAL_POLICIES = ("untrusted", "on-request", "never")

# Our decisions as the approval answers of the v2 thread API.
DECISIONS = {"allow": "accept", "always": "acceptForSession",
             "deny": "decline"}

# JSON-RPC error codes the adapter tells apart.
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_AGENT_VERSION_RE = re.compile(r"/(\d+\.\d+\.\d+)")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


# ---------------------------------------------------------------------
# Ids


def thread_id_of(ref: Any) -> str:
    """The thread uuid in ``ref``, lower-cased, or "".

    SWE knows a Codex session by its rollout file's stem
    (``rollout-2026-11-21T10-00-00-<thread uuid>``, see
    ``sessions.parsers.codex_thread_id``); the wire takes the thread
    uuid, the first uuid in it. A bare uuid is its own thread id."""
    m = _UUID_RE.search(ref) if isinstance(ref, str) else None
    return m.group(0).lower() if m else ""


def session_ref(thread: Any) -> str:
    """The id SWE's session list knows ``thread`` by: its rollout
    file's stem when the server names the file, else the thread id."""
    if not isinstance(thread, dict):
        return ""
    path = text_of(thread.get("path"))
    stem = os.path.basename(path)
    if stem.endswith(".jsonl") and thread_id_of(stem):
        return stem[:-len(".jsonl")]
    return text_of(thread.get("id"))


def agent_version(user_agent: Any) -> str:
    """The CLI version in initialize's ``userAgent``
    (``<client>/0.170.0 (Mac OS ...)``), or ""."""
    m = _AGENT_VERSION_RE.search(user_agent) \
        if isinstance(user_agent, str) else None
    return m.group(1) if m else ""


# ---------------------------------------------------------------------
# Lines we write


def encode(message: dict) -> bytes:
    """One wire line."""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict | None:
    """One line the server printed as a message, or None for a blank
    line, something that is not JSON, or JSON that is not an object."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def request(rpc_id: Any, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method,
            "params": params if params is not None else {}}


def notification(method: str) -> dict:
    return {"jsonrpc": "2.0", "method": method}


def result(rpc_id: Any, body: dict) -> dict:
    """Our answer to one of the server's requests."""
    return {"jsonrpc": "2.0", "id": rpc_id, "result": body}


def error(rpc_id: Any, message: str, code: int = METHOD_NOT_FOUND) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message}}


def initialize(rpc_id: Any, version: str) -> dict:
    return request(rpc_id, "initialize", {
        "clientInfo": {"name": "swe", "title": "SWE", "version": version}})


def initialized() -> dict:
    return notification("initialized")


def _thread_fields(*, cwd: str = "", model: str = "",
                   approval_policy: str = "") -> dict:
    out: dict = {}
    if cwd:
        out["cwd"] = cwd
    if model:
        out["model"] = model
    if approval_policy in APPROVAL_POLICIES:
        out["approvalPolicy"] = approval_policy
    return out


def thread_start(rpc_id: Any, **fields: str) -> dict:
    return request(rpc_id, "thread/start", _thread_fields(**fields))


def thread_resume(rpc_id: Any, thread_id: str, **fields: str) -> dict:
    """Load a thread from disk. ``excludeTurns``: the keeper's log is
    the history, so the server need not send it again."""
    return request(rpc_id, "thread/resume", {
        "threadId": thread_id, "excludeTurns": True,
        **_thread_fields(**fields)})


def thread_fork(rpc_id: Any, thread_id: str, **fields: str) -> dict:
    return request(rpc_id, "thread/fork", {
        "threadId": thread_id, "excludeTurns": True,
        **_thread_fields(**fields)})


def user_input(text: str, attachments: Iterable[str] = ()) -> list:
    """A user message as the ``input`` array: images as ``localImage``
    items, any other file named in the text."""
    items: list = []
    others = []
    for path in attachments:
        path = str(path)
        if path.lower().endswith(_IMAGE_EXT):
            items.append({"type": "localImage", "path": path})
        else:
            others.append(f"[Attached file: {path}]")
    body = " ".join([*others, text]) if others else text
    return [{"type": "text", "text": body, "text_elements": []}, *items]


def client_id(rpc_id: Any) -> str:
    """The ``clientUserMessageId`` we give the message of request
    ``rpc_id``; the server echoes it as the ``userMessage`` item's
    ``clientId``, which is how a steer's landing is recognised."""
    return f"swe-{rpc_id}"


def turn_start(rpc_id: Any, thread_id: str, text: str, *,
               attachments: Iterable[str] = (), model: str = "",
               effort: str = "", approval_policy: str = "") -> dict:
    """One turn. ``model``, ``effort`` and ``approvalPolicy`` are
    overrides the server keeps for this turn and the ones after."""
    params: dict = {"threadId": thread_id,
                    "clientUserMessageId": client_id(rpc_id),
                    "input": user_input(text, attachments)}
    if model:
        params["model"] = model
    if effort:
        params["effort"] = effort
    if approval_policy in APPROVAL_POLICIES:
        params["approvalPolicy"] = approval_policy
    return request(rpc_id, "turn/start", params)


def turn_steer(rpc_id: Any, thread_id: str, turn_id: str, text: str, *,
               attachments: Iterable[str] = ()) -> dict:
    return request(rpc_id, "turn/steer", {
        "threadId": thread_id, "expectedTurnId": turn_id,
        "clientUserMessageId": client_id(rpc_id),
        "input": user_input(text, attachments)})


def turn_interrupt(rpc_id: Any, thread_id: str, turn_id: str) -> dict:
    return request(rpc_id, "turn/interrupt",
                   {"threadId": thread_id, "turnId": turn_id})


def thread_revert(rpc_id: Any, thread_id: str, before_turn_id: str) -> dict:
    """Drop ``before_turn_id`` and every later turn from the thread's
    history. Conversation only: files are not touched."""
    return request(rpc_id, "thread/revert",
                   {"threadId": thread_id, "beforeTurnId": before_turn_id})


def approval_reply(rpc_id: Any, decision: str) -> dict:
    """The answer to a command or file-change approval: ``decision`` is
    one of ``DECISIONS``' values."""
    return result(rpc_id, {"decision": decision})


# ---------------------------------------------------------------------
# What the server's items mean


_ITEM_STATUS = {"completed": "ok", "failed": "error", "declined": "cancelled",
                "interrupted": "cancelled", "inProgress": "ok"}
_PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress",
                "completed": "completed"}
_TURN_STATUS = {"completed": "completed", "interrupted": "interrupted",
                "failed": "failed"}
_EDIT_KIND = {"add": "add", "delete": "delete", "update": "modify"}

# Tool calls remembered past their turn's end, at most.
_TOOLS_MAX = 200

_ALLOW = {"id": "allow", "label": "Allow", "kind": "allow"}
_ALWAYS = {"id": "always", "label": "Allow for this session",
           "kind": "always", "standing": True}
_DENY = {"id": "deny", "label": "Deny", "kind": "deny"}


def _command_text(item: dict) -> str:
    """The command as the model wrote it: the first parsed action's,
    else the full shell line."""
    actions = item.get("commandActions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and text_of(action.get("command")):
                return action["command"]
    return text_of(item.get("command"))


def _change_kind(change: dict) -> tuple[str, str]:
    """``(kind, move_path)`` of one ``FileUpdateChange``."""
    kind = change.get("kind")
    if isinstance(kind, dict):
        return _EDIT_KIND.get(text_of(kind.get("type")), "modify"), \
            text_of(kind.get("move_path"))
    return _EDIT_KIND.get(text_of(kind), "modify"), ""


def _as_diff(kind: str, body: str) -> str:
    """A change's ``diff`` as a unified diff: an add or delete carries
    the file's content instead, which becomes one hunk."""
    if kind == "modify" or body.startswith("@@") or not body:
        return body
    lines = body.splitlines()
    sign, header = ("+", f"@@ -0,0 +1,{len(lines)} @@") if kind == "add" \
        else ("-", f"@@ -1,{len(lines)} +0,0 @@")
    return header + "\n" + "".join(f"{sign}{ln}\n" for ln in lines)


def file_edits(item: Any) -> list | None:
    """``file_edits`` from a ``fileChange`` item's ``changes``."""
    changes = item.get("changes") if isinstance(item, dict) else None
    if not isinstance(changes, list):
        return None
    edits = []
    for change in changes:
        if not isinstance(change, dict) or not text_of(change.get("path")):
            continue
        kind, moved = _change_kind(change)
        edit: dict[str, Any] = {"path": moved or change["path"],
                                "kind": kind}
        if moved:
            edit["old_path"] = change["path"]
        diff = _as_diff(kind, text_of(change.get("diff")))
        if diff:
            edit["diff"] = diff
        edits.append(edit)
    return redact(edits) or None


def tool_card(item: dict) -> tuple[str, Any] | None:
    """``(name, input)`` for an item shown as a tool call, else None."""
    kind = item.get("type")
    if kind == "commandExecution":
        return "shell", {"command": _command_text(item),
                         "cwd": text_of(item.get("cwd"))}
    if kind == "fileChange":
        changes = [c for c in item.get("changes") or ()
                   if isinstance(c, dict)]
        return "apply_patch", {
            "path": text_of(changes[0].get("path")) if changes else "",
            "changes": [{"path": text_of(c.get("path")),
                         "kind": _change_kind(c)[0]} for c in changes]}
    if kind == "mcpToolCall":
        return (f"{text_of(item.get('server')) or 'mcp'}."
                f"{text_of(item.get('tool')) or 'tool'}",
                item.get("arguments"))
    if kind == "dynamicToolCall":
        return text_of(item.get("tool")) or "tool", item.get("arguments")
    if kind == "webSearch":
        return "web_search", {"query": text_of(item.get("query"))}
    if kind == "imageView":
        return "view_image", {"path": text_of(item.get("path"))}
    if kind == "collabAgentToolCall":
        return f"agent.{text_of(item.get('tool')) or 'call'}", {
            "prompt": text_of(item.get("prompt"))}
    return None


def _tool_output(item: dict) -> str:
    kind = item.get("type")
    if kind == "commandExecution":
        return text_of(item.get("aggregatedOutput"))
    if kind in ("mcpToolCall", "dynamicToolCall"):
        failure = item.get("error")
        if isinstance(failure, dict) and text_of(failure.get("message")):
            return failure["message"]
        result_ = item.get("result")
        content = result_.get("content") if isinstance(result_, dict) \
            else item.get("contentItems")
        parts = [text_of(b.get("text")) for b in content or ()
                 if isinstance(b, dict)]
        return "\n".join(p for p in parts if p)
    return ""


def _error_code(info: Any) -> str:
    """``codexErrorInfo`` as one short code."""
    if isinstance(info, str) and info:
        return info
    if isinstance(info, dict) and info:
        return str(next(iter(info)))
    return "error"


@dataclass
class _Pending:
    """One of the server's requests waiting on us."""

    rpc_id: Any
    method: str
    params: dict
    always: bool = False
    question_id: str = ""
    options: list = field(default_factory=list)


@dataclass
class _Expected:
    """A ``turn/start`` or ``turn/steer`` written, its answer not yet
    read: the turn opens (the message joins it) when the server accepts
    it, in the reader's own order."""

    steer: bool
    text: str
    attachments: tuple = ()
    command_id: str = ""
    device: str = ""
    client_id: str = ""


# ---------------------------------------------------------------------
# The session state machine


class ThreadState(SessionCore):
    """One Codex thread's protocol state, from the adapter's side.

    The adapter calls ``start_events`` after the handshake,
    ``expect_turn`` before it writes ``turn/start`` and ``expect_steer``
    before ``turn/steer``, ``answer`` per ``respond``,
    ``interrupt_sent`` per ``interrupt``, ``feed`` per line the server
    prints, and ``exited`` or ``closed`` once at the end.

    Line by line (``feed``):

    ===========================================  =========================
    Server line                                  v2 events
    ===========================================  =========================
    an answer to ``turn/start``                  ``turn.started``,
                                                 ``session.state``
                                                 running, the user's
                                                 ``message.completed``
    an answer to ``turn/steer``                  the user's
                                                 ``message.completed``
    any other answer                             an ``Ack`` (no event)
    ``item/*/requestApproval`` (a request)       ``approval.requested``,
                                                 ``session.state`` waiting
    ``item/tool/requestUserInput`` (a request)   ``input.requested``
    any other request                            an error answer
    ``serverRequest/resolved``                   ``approval.resolved``
                                                 (``harness``) if still
                                                 open
    ``turn/started``                             ``turn.started`` when no
                                                 turn is open
    ``item/started`` / ``item/completed``        ``tool.started`` /
                                                 ``tool.completed``,
                                                 ``message.completed``,
                                                 ``thinking.completed``
    ``item/agentMessage/delta``, reasoning       ``message.delta``
    deltas
    ``turn/plan/updated``                        ``plan.updated``
    ``thread/tokenUsage/updated``                ``usage.updated``
    ``thread/compacted``                         ``context.compacted``
    ``error`` (not ``willRetry``)                ``error``
    ``turn/completed``                           open streams closed,
                                                 requests retired,
                                                 ``turn.ended``,
                                                 ``session.state`` idle
    anything else                                nothing (``unknown``)
    ===========================================  =========================

    A notification that names another thread (a sub-agent's) is
    skipped.
    """

    def __init__(self, session: str, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.thread_id = ""
        self.model = ""
        # our turn id -> the server's, for ``thread/revert``
        self.native_turns: dict[str, str] = {}
        self._expected: dict[Any, _Expected] = {}
        # rpc id of an accepted turn/start or turn/steer -> our turn id
        self.accepted: dict[Any, str] = {}
        self._tools: dict[str, str] = {}  # item id -> tool name
        # fileChange item id -> the paths it touches, for its approval
        self._changes: dict[str, list] = {}
        self._errored = False
        # Steers the server accepted whose message has not landed in
        # the turn yet, oldest first; and the client ids of the
        # ``userMessage`` items seen this turn.
        self._steers: list[_Expected] = []
        self._landed: set = set()

    # -- calls from the adapter ------------------------------------------

    @property
    def busy(self) -> bool:
        """A turn is open or one is being started."""
        return self.turn is not None or any(
            not e.steer for e in self._expected.values())

    def expect_turn(self, rpc_id: Any, text: str, *,
                    attachments: Iterable[str] = (), command_id: str = "",
                    device: str = "") -> None:
        self._expected[rpc_id] = _Expected(
            False, text, tuple(attachments), command_id, device,
            client_id(rpc_id))

    def expect_steer(self, rpc_id: Any, text: str, *,
                     attachments: Iterable[str] = (), command_id: str = "",
                     device: str = "") -> None:
        self._expected[rpc_id] = _Expected(
            True, text, tuple(attachments), command_id, device,
            client_id(rpc_id))

    def forget(self, rpc_id: Any) -> None:
        """The request was never written, or never answered."""
        self._expected.pop(rpc_id, None)

    def moved(self, thread: Any) -> list:
        """The server named the thread again (the answer to
        ``thread/revert``): a revert writes a new rollout file, so the
        id SWE's session list knows the thread by changes with it. Says
        so with a ``session.state`` carrying the new id; nothing when it
        did not change."""
        ref = session_ref(thread)
        if self.done or not ref or ref == self.session_id:
            return []
        self.session_id = ref
        return [ev.session_state(self.key, "idle", harness_session_id=ref,
                                 ts=self.now())]

    def turn_cleared(self) -> None:
        # ``_tools`` is kept: a command can outlive its turn (a steer
        # leaves it running), and its completion still closes its row.
        while len(self._tools) > _TOOLS_MAX:
            self._tools.pop(next(iter(self._tools)))
        self._changes.clear()
        self._errored = False
        self._steers.clear()
        self._landed.clear()

    def answer(self, request_id: str, answer: Answer) -> tuple[dict, list]:
        """Our answer to an open request and the ``approval.resolved``
        it closes. Raises UnknownRequest."""
        pending: _Pending = self.take(request_id)
        if pending.method == "item/tool/requestUserInput":
            text = answer.text
            if not text and answer.option_id:
                text = next((o["label"] for o in pending.options
                             if o["id"] == answer.option_id),
                            answer.option_id)
            reply = result(pending.rpc_id, {"answers": {
                pending.question_id: {"answers": [text] if text else []}}})
            return reply, self.answered(request_id, answer)
        decision = answer.decision or (answer.option_id if answer.option_id
                                       in DECISIONS else "")
        if not decision:
            # A bare text answer is feedback: the call does not run.
            decision = "deny"
        if decision == "always" and not pending.always:
            decision = "allow"
        if pending.method == "item/permissions/requestApproval":
            granted = pending.params.get("permissions") \
                if decision != "deny" else {}
            reply = result(pending.rpc_id, {
                "permissions": granted if isinstance(granted, dict) else {},
                "scope": "session" if decision == "always" else "turn"})
        else:
            reply = approval_reply(pending.rpc_id, DECISIONS[decision])
        return reply, self.answered(request_id, answer, decision)

    # -- lines from the server -------------------------------------------

    def feed(self, msg: dict) -> Fed:
        fed = Fed()
        if self.done or not isinstance(msg, dict):
            return fed
        method = msg.get("method")
        if isinstance(method, str) and method:
            params = msg.get("params")
            params = params if isinstance(params, dict) else {}
            if "id" in msg:
                self._on_request(msg["id"], method, params, fed)
            else:
                self._on_notification(method, params, fed)
        elif "id" in msg:
            self._on_answer(msg, fed)
        else:
            self.note_unknown("?")
        return fed

    def _on_answer(self, msg: dict, fed: Fed) -> None:
        rpc_id = msg["id"]
        failure = msg.get("error")
        if isinstance(failure, dict):
            ack = Ack(rpc_id, False, error=text_of(failure.get("message"))
                      or "error", code=failure.get("code"))
        else:
            body = msg.get("result")
            ack = Ack(rpc_id, True, body if isinstance(body, dict) else {})
        expected = self._expected.pop(rpc_id, None)
        if expected is not None and ack.ok:
            self._accepted(rpc_id, expected, ack.body, fed.events)
        fed.acks.append(ack)

    def _accepted(self, rpc_id: Any, expected: _Expected, body: dict,
                  out: list) -> None:
        """The server took a ``turn/start`` or ``turn/steer``."""
        if expected.steer:
            if self.turn is None:
                return  # the turn ended first; the server answered late
            self.accepted[rpc_id] = self.turn.turn_id
            if expected.client_id in self._landed:
                self._steered(out, expected)  # its item overtook the answer
            else:
                self._steers.append(expected)
            return
        turn = body.get("turn")
        native = text_of(turn.get("id")) if isinstance(turn, dict) else ""
        if self.turn is None:
            self.open_turn(out, by=expected.device, text=expected.text,
                           command_id=expected.command_id)
        assert self.turn is not None
        self.turn.command_id = expected.command_id
        self.out(out, self.user_message(
            expected.text, attachments=expected.attachments,
            command_id=expected.command_id, device=expected.device))
        self._native(native)
        self.accepted[rpc_id] = self.turn.turn_id

    def _steered(self, out: list, steer: _Expected) -> None:
        """A steer's message joined the turn: whatever was streaming is
        cut there (the server abandons the reply in progress and sends
        no completion for it), then the user's ``message.completed``."""
        self.close_streams(out)
        self.out(out, self.user_message(
            steer.text, attachments=steer.attachments,
            command_id=steer.command_id, device=steer.device))

    def _native(self, native: str) -> None:
        if self.turn is not None and native:
            self.turn.native_id = native
            self.native_turns[self.turn.turn_id] = native

    def _mine(self, params: dict) -> bool:
        thread = params.get("threadId")
        return not isinstance(thread, str) or not self.thread_id \
            or thread == self.thread_id

    def _ensure_turn(self, out: list) -> None:
        """A turn the server runs that we did not start (or whose
        ``turn/start`` answer is still on its way)."""
        if self.turn is not None:
            return
        for rpc_id, expected in list(self._expected.items()):
            if not expected.steer:
                # Its notifications overtook its answer: open it now.
                del self._expected[rpc_id]
                self._accepted(rpc_id, expected, {}, out)
                return
        self.open_turn(out)

    # -- the server's requests -------------------------------------------

    def _on_request(self, rpc_id: Any, method: str, params: dict,
                    fed: Fed) -> None:
        request_id = f"{self.run_id}-{rpc_id}"
        item_id = text_of(params.get("itemId"))
        reason = text_of(params.get("reason"))
        if method == "item/commandExecution/requestApproval":
            self._ensure_turn(fed.events)
            # "Always" only where the server itself offers it for this
            # command. (Its other standing choice,
            # ``acceptWithExecpolicyAmendment``, writes a rule into the
            # user's Codex config and is never offered from here.)
            offered = params.get("availableDecisions")
            always = isinstance(offered, list) \
                and "acceptForSession" in offered
            prompt = text_of(params.get("command")) or "run a command"
            net = params.get("networkApprovalContext")
            if isinstance(net, dict) and text_of(net.get("host")):
                prompt += f"\n\nNetwork access: {net['host']}"
            if reason:
                prompt += f"\n\n{reason}"
            self.ask(fed.events, request_id, "command", prompt,
                     _Pending(rpc_id, method, params, always),
                     options=[_ALLOW, *([_ALWAYS] if always else []), _DENY],
                     call_id=item_id)
        elif method == "item/fileChange/requestApproval":
            self._ensure_turn(fed.events)
            prompt = "Apply file changes"
            root = text_of(params.get("grantRoot"))
            if root:
                prompt += f" under {root}"
            paths = self._changes.get(item_id)
            if paths:
                prompt += ":\n" + "\n".join(paths[:20])
            if reason:
                prompt += f"\n\n{reason}"
            # No "always": ``acceptForSession`` did not stop the next
            # file change from asking (the ``always`` recording).
            self.ask(fed.events, request_id, "tool", prompt,
                     _Pending(rpc_id, method, params),
                     options=[_ALLOW, _DENY], call_id=item_id)
        elif method == "item/permissions/requestApproval":
            self._ensure_turn(fed.events)
            wanted = json.dumps(jsonable(params.get("permissions")),
                                sort_keys=True)
            prompt = (reason or "Grant more permissions") + f"\n\n{wanted}"
            self.ask(fed.events, request_id, "tool", prompt,
                     _Pending(rpc_id, method, params, True),
                     options=[_ALLOW, _ALWAYS, _DENY], call_id=item_id)
        elif method == "item/tool/requestUserInput" \
                and self._one_question(params):
            self._ensure_turn(fed.events)
            question = params["questions"][0]
            options = [{"id": text_of(o.get("label")),
                        "label": text_of(o.get("label"))}
                       for o in question.get("options") or ()
                       if isinstance(o, dict) and text_of(o.get("label"))]
            self.ask_input(
                fed.events, request_id, text_of(question.get("question")),
                _Pending(rpc_id, method, params,
                         question_id=text_of(question.get("id")),
                         options=options), options=options)
        else:
            # Nothing we serve: answer, so the server never waits on us.
            self.note_unknown(method)
            fed.replies.append(error(rpc_id, f"{method} is not supported "
                                             "by this client"))

    @staticmethod
    def _one_question(params: dict) -> bool:
        questions = params.get("questions")
        return isinstance(questions, list) and len(questions) == 1 \
            and isinstance(questions[0], dict) \
            and bool(text_of(questions[0].get("id")))

    # -- the server's notifications --------------------------------------

    def _on_notification(self, method: str, params: dict, fed: Fed) -> None:
        if not self._mine(params):
            self.note_unknown("other-thread")
            return
        handler = _NOTIFICATIONS.get(method)
        if handler is None:
            self.note_unknown(method)
            return
        handler(self, params, fed.events)

    def _on_turn_started(self, params: dict, out: list) -> None:
        turn = params.get("turn")
        self._ensure_turn(out)
        self._native(text_of(turn.get("id")) if isinstance(turn, dict)
                     else "")

    def _on_turn_completed(self, params: dict, out: list) -> None:
        turn = params.get("turn")
        turn = turn if isinstance(turn, dict) else {}
        mine = self.turn
        if mine is None:
            return
        native = text_of(turn.get("id"))
        if mine.native_id and native and native != mine.native_id:
            return  # an older turn's end, late
        status = _TURN_STATUS.get(text_of(turn.get("status")), "completed")
        failure = turn.get("error")
        if status == "failed":
            message = text_of(failure.get("message")) \
                if isinstance(failure, dict) else ""
            self._fail(out, failure if isinstance(failure, dict) else {},
                       message or "the turn failed")
        for steer in self._steers[:]:
            # Accepted, never announced as an item: it still was sent.
            self._steered(out, steer)
        self.finish_turn(out, status)

    def _fail(self, out: list, failure: dict, message: str) -> None:
        turn = self.turn
        if turn is None or self._errored:
            return
        self._errored = True
        turn.error = True
        turn.reason = redact(message)[:300]
        self.out(out, ev.error(
            self.key, redact(_error_code(failure.get("codexErrorInfo"))),
            turn.reason, turn_id=turn.turn_id, ts=self.now()))

    def _on_error(self, params: dict, out: list) -> None:
        failure = params.get("error")
        if params.get("willRetry") is True or not isinstance(failure, dict) \
                or self.turn is None:
            return
        self._fail(out, failure, text_of(failure.get("message"))
                   or "the turn failed")

    def _on_item_started(self, params: dict, out: list) -> None:
        item = params.get("item")
        if not isinstance(item, dict) or not text_of(item.get("id")):
            return
        kind = item.get("type")
        if kind == "userMessage":
            self._user_item(text_of(item.get("clientId")), out)
            return
        self._ensure_turn(out)
        if kind == "agentMessage":
            self.stream(item["id"], "assistant")
        elif kind == "reasoning":
            self.stream(item["id"], "thinking")
        else:
            self._tool_started(item, out)

    def _user_item(self, cid: str, out: list) -> None:
        """A ``userMessage`` item: the turn's own first message (nothing
        to do, ``turn/start``'s answer announced it) or a steer landing,
        matched by the client id we gave it; a server that echoes none
        gets the oldest steer waiting."""
        if self.turn is None:
            return
        if cid:
            self._landed.add(cid)
        for i, steer in enumerate(self._steers):
            if not cid or steer.client_id == cid:
                del self._steers[i]
                self._steered(out, steer)
                return

    def _tool_started(self, item: dict, out: list) -> bool:
        card = tool_card(item)
        if card is None:
            return False
        name, tool_input = card
        name = redact(name)
        self._tools[item["id"]] = name
        if item.get("type") == "fileChange":
            self._changes[item["id"]] = [
                c["path"] for c in tool_input["changes"] if c["path"]]
        self.out(out, ev.tool_started(
            self.key, item["id"], name, summary=tool_summary(name, tool_input),
            input=jsonable(redact(tool_input)), turn_id=self.tid(),
            ts=self.now()))
        return True

    def _on_item_completed(self, params: dict, out: list) -> None:
        item = params.get("item")
        if not isinstance(item, dict) or not text_of(item.get("id")):
            return
        kind = item.get("type")
        iid = item["id"]
        if kind == "userMessage":
            return
        if self.turn is None:
            # Past its turn's end: only a tool call still open counts.
            if iid in self._tools:
                self._tool_completed(item, out)
            return
        if kind == "agentMessage":
            text = text_of(item.get("text"))
            if text or self._streamed(iid):
                self.complete(out, iid, "assistant", text)
            else:
                self.streams.pop(iid, None)  # an empty commentary item
        elif kind == "reasoning":
            parts = [p for key in ("summary", "content")
                     for p in item.get(key) or () if isinstance(p, str) and p]
            if parts or self._streamed(iid):
                self.complete(out, iid, "thinking", "\n\n".join(parts))
            else:
                self.streams.pop(iid, None)
        elif kind == "plan":
            self.out(out, ev.plan_updated(
                self.key, [], text=redact(text_of(item.get("text"))),
                turn_id=self.tid(), ts=self.now()))
        elif kind == "contextCompaction":
            self._on_compacted(params, out)
        elif iid in self._tools or self._tool_started(item, out):
            self._tool_completed(item, out)

    def _tool_completed(self, item: dict, out: list) -> None:
        name = self._tools.pop(item["id"])
        status = _ITEM_STATUS.get(text_of(item.get("status")), "ok")
        self.out(out, ev.tool_completed(
            self.key, item["id"], status, name=name,
            output=clip(redact(_tool_output(item))),
            file_edits=file_edits(item) if item.get("type") == "fileChange"
            and status == "ok" else None,
            turn_id=self.tid(), ts=self.now()))

    def _streamed(self, sid: str) -> bool:
        block = self.streams.get(sid)
        return block is not None and bool(block.raw)

    def _on_message_delta(self, params: dict, out: list) -> None:
        if self.turn is not None:
            self.delta(out, text_of(params.get("itemId")), "assistant",
                       text_of(params.get("delta")))

    def _on_reasoning_delta(self, params: dict, out: list) -> None:
        if self.turn is not None:
            self.delta(out, text_of(params.get("itemId")), "thinking",
                       text_of(params.get("delta")))

    def _on_reasoning_part(self, params: dict, out: list) -> None:
        # A new summary part: the completed text joins parts with a
        # blank line, so the stream does too.
        block = self.streams.get(text_of(params.get("itemId")))
        if block is not None and block.raw:
            block.raw += "\n\n"

    def _on_plan(self, params: dict, out: list) -> None:
        if self.turn is None:
            return
        items = [{"text": redact(text_of(step.get("step"))),
                  "status": _PLAN_STATUS.get(text_of(step.get("status")),
                                             "pending")}
                 for step in params.get("plan") or ()
                 if isinstance(step, dict)]
        note = text_of(params.get("explanation"))
        self.out(out, ev.plan_updated(
            self.key, items, text=redact(note) if note else None,
            turn_id=self.tid(), ts=self.now()))

    def _on_usage(self, params: dict, out: list) -> None:
        usage = params.get("tokenUsage")
        last = usage.get("last") if isinstance(usage, dict) else None
        if not isinstance(last, dict):
            return
        counts = {name: count(last.get(key)) for name, key in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("cache_read_tokens", "cachedInputTokens"),
            ("cache_write_tokens", "cacheWriteInputTokens"),
            ("context_tokens", "totalTokens"))}
        window = count(usage.get("modelContextWindow"))
        if window:
            counts["context_window"] = window
        counts = {k: v for k, v in counts.items() if v is not None}
        if counts:
            self.out(out, ev.usage_updated(self.key, turn_id=self.tid(),
                                           ts=self.now(), **counts))

    def _on_compacted(self, params: dict, out: list) -> None:
        self.out(out, ev.context_compacted(self.key, turn_id=self.tid(),
                                           ts=self.now()))

    def _on_request_resolved(self, params: dict, out: list) -> None:
        rpc_id = params.get("requestId")
        for request_id, pending in list(self.pending.items()):
            if pending.rpc_id == rpc_id:
                self.withdrawn(out, request_id)


_NOTIFICATIONS = {
    "turn/started": ThreadState._on_turn_started,
    "turn/completed": ThreadState._on_turn_completed,
    "error": ThreadState._on_error,
    "item/started": ThreadState._on_item_started,
    "item/completed": ThreadState._on_item_completed,
    "item/agentMessage/delta": ThreadState._on_message_delta,
    "item/reasoning/summaryTextDelta": ThreadState._on_reasoning_delta,
    "item/reasoning/textDelta": ThreadState._on_reasoning_delta,
    "item/reasoning/summaryPartAdded": ThreadState._on_reasoning_part,
    "turn/plan/updated": ThreadState._on_plan,
    "thread/tokenUsage/updated": ThreadState._on_usage,
    "thread/compacted": ThreadState._on_compacted,
    "serverRequest/resolved": ThreadState._on_request_resolved,
}


# ---------------------------------------------------------------------
# The pre-keeper channel (``inject.CodexAppServerInjector``)
#
# One prompt at a time into a session started in a terminal, its
# transcript still read from the rollout file: only approvals, live
# text and the turn id are taken off the wire. It builds its lines and
# reads the server's here, so the method names live in one module. It
# goes away with the tmux paths (G22, G24).

TURN_START_METHODS = ("turn/started", "codex/turnStarted")
TURN_END_METHODS = ("turn/completed", "turn/failed", "codex/turnCompleted")


def is_approval_request(method: str) -> bool:
    """Whether a server request asks for an approval (the v2 thread
    API's ``item/*/requestApproval`` and the older ``*Approval``)."""
    return "requestApproval" in method or "Approval" in method


def approval_prompt(method: str, params: dict) -> str:
    """One line saying what an approval request is about."""
    return str(params.get("reason") or params.get("command")
               or params.get("itemId") or method)


def live_text(method: str, params: dict) -> tuple[str, str, str] | None:
    """What a notification means for a reply being streamed:
    ``("delta", item id, text)``, ``("done", item id, "")`` when the
    item is complete, ``("turn_done", "", "")`` when the turn is, else
    None. Reasoning deltas are left out: a reasoning item often never
    reaches the rollout as text."""
    if method == "item/agentMessage/delta":
        item_id = str(params.get("itemId") or "")
        delta = params.get("delta")
        if item_id and isinstance(delta, str) and delta:
            return "delta", item_id, delta
    elif method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict) and item.get("id"):
            return "done", str(item["id"]), ""
    elif method in TURN_END_METHODS:
        return "turn_done", "", ""
    return None


def started_turn_id(method: str, params: dict) -> str | None:
    """The id of the turn a notification starts, "" when it ends one,
    None for any other notification."""
    if method in TURN_START_METHODS:
        turn = params.get("turn") or {}
        return str(turn["id"]) if isinstance(turn, dict) and turn.get("id") \
            else None
    if method in TURN_END_METHODS:
        return ""
    return None


def plain_turn_params(thread_id: str, text: str, *, model: str = "",
                      effort: str = "") -> dict:
    """``turn/start`` params as the pre-keeper channel sends them: one
    text item (attachments already folded into ``text``) and the
    session's picks as overrides."""
    params: dict = {"threadId": thread_id,
                    "input": [{"type": "text", "text": text}]}
    if model:
        params["model"] = model
    if effort:
        params["effort"] = effort
    return params


def interrupt_params(thread_id: str, turn_id: str = "") -> dict:
    params = {"threadId": thread_id}
    if turn_id:
        params["turnId"] = turn_id
    return params


# ---------------------------------------------------------------------
# Replaying a recorded fixture


_THREAD_METHODS = ("thread/start", "thread/resume", "thread/fork")


def _input_text(params: dict) -> str:
    for item in params.get("input") or ():
        if isinstance(item, dict) and item.get("type") == "text":
            return text_of(item.get("text"))
    return ""


def _answer_from_reply(body: Any) -> Answer:
    """The ``Answer`` one of our recorded replies stands for."""
    body = body if isinstance(body, dict) else {}
    if "answers" in body:
        first = next(iter(body["answers"].values()), {})
        return Answer(text=" ".join(first.get("answers") or ()) or "-")
    if "permissions" in body:
        if not body["permissions"]:
            return Answer(decision="deny")
        return Answer(decision="always" if body.get("scope") == "session"
                      else "allow")
    decision = body.get("decision")
    for ours, theirs in DECISIONS.items():
        if decision == theirs:
            return Answer(decision=ours)
    return Answer(decision="deny")


def replay(lines: list[dict], *, session: str = FIXTURE_SESSION) -> list:
    """The v2 events a recorded fixture comes to, through the same
    :class:`ThreadState` calls the adapter makes: each process of the
    fixture in turn, our lines driving ``expect_turn``, ``expect_steer``,
    ``answer`` and ``interrupt_sent``, the server's going to ``feed``,
    and the recorder's closing stdin ending it with ``closed``.
    Timestamps are the fixture's relative times on a fixed epoch."""
    header = lines[0] if lines and "fixture" in lines[0] else {}
    now = [0.0]

    def clock() -> float:
        return round(FIXTURE_T0 + now[0], 3)

    out: list = []
    states: dict[int, ThreadState] = {}
    thread_rpc: dict[int, Any] = {}
    reverts: set = set()
    for line in lines[1:] if header else lines:
        proc = line.get("proc", 0)
        if isinstance(line.get("t"), (int, float)):
            now[0] = float(line["t"])
        state = states.get(proc)
        if state is None:
            state = states[proc] = ThreadState(
                session, clock=clock, run_id=f"{proc + 1:08x}")
        if "exit" in line:
            out.extend(state.closed())
            continue
        msg = line.get("msg")
        if not isinstance(msg, dict):
            continue
        if line.get("dir") == "out":
            if msg.get("method") == "thread/revert":
                reverts.add((proc, msg.get("id")))
            out.extend(_replay_outgoing(state, msg, thread_rpc, proc))
            continue
        fed = state.feed(msg)
        for ack in fed.acks:
            if ack.ok and ack.request_id == thread_rpc.get(proc) \
                    and not state.started:
                thread = ack.body.get("thread")
                state.thread_id = text_of(thread.get("id")) \
                    if isinstance(thread, dict) else ""
                out.extend(state.start_events(ALL_CAPS.narrowed(PROVEN),
                                              session_ref(thread)))
            elif ack.ok and (proc, ack.request_id) in reverts:
                # What ``CodexAppServerAdapter.rollback`` emits.
                out.extend(state.moved(ack.body.get("thread")))
        out.extend(fed.events)
    return out


def _replay_outgoing(state: ThreadState, msg: dict, thread_rpc: dict,
                     proc: int) -> list:
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method in _THREAD_METHODS and proc not in thread_rpc:
        thread_rpc[proc] = msg.get("id")
    elif method == "turn/start":
        state.expect_turn(msg.get("id"), _input_text(params))
    elif method == "turn/steer":
        state.expect_steer(msg.get("id"), _input_text(params))
    elif method == "turn/interrupt":
        return state.interrupt_sent()
    elif method is None and "id" in msg and "result" in msg:
        for request_id, pending in list(state.pending.items()):
            if pending.rpc_id == msg["id"]:
                return state.answer(request_id,
                                    _answer_from_reply(msg["result"]))[1]
    return []
