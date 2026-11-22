"""The ACP session adapter: one Agent Client Protocol agent per session.

Every harness that speaks ACP (Gemini, Qwen, Kimi, Devin, Cursor,
Copilot) runs through this one adapter. The wire is
``swe.remote.acp``: its ``Connection`` owns the agent process, the
JSON-RPC plumbing, the handshake and the reader thread, as it does for
the pre-keeper channel. This module adds the two things a keeper needs
on top:

- :class:`AcpState`, pure: what the agent's ``session/update``
  notifications, permission requests and prompt answers mean as v2
  events (``events.py``), on the shared ``SessionCore``. No process, no
  thread, so a recorded fixture replays through the code the live
  adapter runs (:func:`replay`).
- :class:`AcpAdapter`: a ``Connection`` subclass that hands every
  message to the state machine instead of the hub's v1 sinks, with a
  bounded writer, under the contract in ``base.py``.

- ``start`` runs ``initialize`` then ``session/new`` (a launch) or
  ``session/load`` (a resume, for an agent that advertises
  ``loadSession``), and returns the agent's session id. The agent
  names sessions, so ``StartSpec.session_id`` is not used.
- ``send_turn`` writes ``session/prompt``; the turn ends when its
  answer arrives (``stopReason``).
- ``interrupt`` writes ``session/cancel`` and answers every open
  permission request ``cancelled``, as the protocol requires.
- ``respond`` writes the JSON-RPC answer to an open
  ``session/request_permission``.
- ``set_option``: ``permission_mode`` and ``plan_mode`` are
  ``session/set_mode`` with one of the modes the agent listed;
  ``model`` is ``session/set_model`` with one of its models.
- ``steer``, ``rollback``, ``stop_task`` and fork have no request in
  the protocol and stay off.
- ``close`` retires what is open, ends the turn, emits ``closed``, then
  closes stdin (agents exit on EOF), and terminates and kills one that
  lingers.

The caps in force are harness.json's, met with what the fixtures prove
(``PROVEN``), met with what this agent advertised in its handshake: no
``plan`` mode listed, no plan mode offered.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from swe.remote import acp as wire
from swe.remote.adapters import events as ev
from swe.remote.adapters import options as opts
from swe.remote.adapters import registry
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, EventSink, StartSpec, TranscriptPosition,
    TurnInput, Unsupported, check_option,
)
from swe.remote.adapters.claude_wire import version_ok
from swe.remote.adapters.core import (
    ALL_CAPS, FIXTURE_SESSION, FIXTURE_T0, Fed, LineWriter, SessionCore,
    Stalled, clip, count, jsonable, redact, text_of, tool_summary,
)

__all__ = ["AcpAdapter", "AcpState", "PROVEN", "RECORDED", "replay"]

# The caps this adapter can deliver, each proven by a recording of a
# real agent. Steer, rollback, fork and stopping one task have no
# request in ACP v1; ``effort`` has no wire form.
PROVEN = Caps(plan_mode=True,
              options=frozenset({"model", "permission_mode"}))

# The agent and version the real fixtures were recorded against.
RECORDED = ("qwen-code", "0.28.0")

# The mode id every recorded agent uses for "plan, do not act".
PLAN_MODE = "plan"
# Where plan mode off goes when no mode from before it is known: the one
# that asks before edits and commands (qwen's ``default``), and never
# another the agent happens to list first.
FALLBACK_MODE = "default"

# How long the whole handshake, any later request, one stdin write and
# the kill steps get.
INIT_TIMEOUT_S = 90.0
CALL_TIMEOUT_S = 15.0
WRITE_TIMEOUT_S = 10.0
_KILL_WAIT_S = 3.0

_CANCELLED = {"outcome": {"outcome": "cancelled"}}
# PermissionOptionKind -> the v2 option kind.
_OPTION_KINDS = {"allow_once": "allow", "allow_always": "always",
                 "reject_once": "deny", "reject_always": "deny"}
_TOOL_STATUS = {"completed": "ok", "failed": "error"}
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
_STOP_STATUS = {"end_turn": "completed", "cancelled": "interrupted"}
# Tool calls remembered past their turn's end, and diff lines kept.
_TOOLS_MAX = 256
_DIFF_LINES_MAX = 400


# ---------------------------------------------------------------------
# Small pure helpers


def resource_links(attachments: Iterable[str]) -> list:
    """Attached files as ``resource_link`` content blocks (the baseline
    every agent must accept)."""
    return [{"type": "resource_link",
             "uri": "file://" + urllib.parse.quote(str(path)),
             "name": str(path).rsplit("/", 1)[-1]} for path in attachments]


def mode_ids(info: Any) -> list[str]:
    """The mode ids a ``session/new`` or ``session/load`` answer lists."""
    modes = info.get("modes") if isinstance(info, dict) else None
    listed = modes.get("availableModes") if isinstance(modes, dict) else None
    return [m["id"] for m in listed or () if isinstance(m, dict)
            and isinstance(m.get("id"), str) and m["id"]]


def _listed(info: Any, field: str, items: str, key: str) -> list[dict]:
    group = info.get(field) if isinstance(info, dict) else None
    listed = group.get(items) if isinstance(group, dict) else None
    return [opts.choice(m[key], m.get("name"), m.get("description"))
            for m in listed or () if isinstance(m, dict)
            and isinstance(m.get(key), str) and m[key]]


def model_choices(info: Any) -> list[dict]:
    """The models a session answer lists, as ``session.options``
    choices."""
    return _listed(info, "models", "availableModels", "modelId")


def current_model(info: Any) -> str:
    models = info.get("models") if isinstance(info, dict) else None
    return text_of(models.get("currentModelId")) \
        if isinstance(models, dict) else ""


def current_mode(info: Any) -> str:
    modes = info.get("modes") if isinstance(info, dict) else None
    return text_of(modes.get("currentModeId")) \
        if isinstance(modes, dict) else ""


def model_ids(info: Any) -> dict[str, str]:
    """``{name or id as a caller would say it: the agent's modelId}``
    from a session answer's ``models``."""
    models = info.get("models") if isinstance(info, dict) else None
    listed = models.get("availableModels") if isinstance(models, dict) \
        else None
    out: dict[str, str] = {}
    for model in listed or ():
        if not isinstance(model, dict) or not text_of(model.get("modelId")):
            continue
        out[model["modelId"]] = model["modelId"]
        if text_of(model.get("name")):
            out.setdefault(model["name"], model["modelId"])
    return out


def advertised(info: Any) -> Caps:
    """What one agent's session answer lets us offer."""
    modes = mode_ids(info)
    options = set()
    if modes:
        options.add("permission_mode")
    if model_ids(info):
        options.add("model")
    return Caps(plan_mode=PLAN_MODE in modes, options=frozenset(options))


def _content_text(content: Any) -> str:
    """The text of a tool call's ``content`` blocks."""
    parts = []
    for block in content if isinstance(content, list) else ():
        inner = block.get("content") if isinstance(block, dict) else None
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(text_of(inner.get("text")))
    return "\n".join(p for p in parts if p)


def file_edits(content: Any) -> list | None:
    """``file_edits`` from a tool call's ``diff`` content blocks
    (``{type: diff, path, oldText, newText}``)."""
    edits = []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "diff" \
                or not text_of(block.get("path")):
            continue
        old, new = block.get("oldText"), block.get("newText")
        kind = "add" if old in (None, "") and new else (
            "delete" if new in (None, "") and old else "modify")
        # Redacted whole, before the diff cuts them into hunks: a key
        # block is only known by its first and last lines.
        lines = list(difflib.unified_diff(
            redact(text_of(old)).splitlines(),
            redact(text_of(new)).splitlines(),
            lineterm="", n=3))[2:_DIFF_LINES_MAX + 2]
        edit: dict[str, Any] = {"path": block["path"], "kind": kind}
        if lines:
            edit["diff"] = "\n".join(lines) + "\n"
        edits.append(edit)
    return redact(edits) or None


def error_text(err: Any) -> str:
    """What a JSON-RPC error says, its ``data.details`` included (where
    agents put the provider's own message)."""
    if not isinstance(err, dict):
        return redact(text_of(err) or "error")[:300]
    text = text_of(err.get("message")) or str(err.get("code") or "error")
    data = err.get("data")
    details = text_of(data.get("details")) if isinstance(data, dict) \
        else text_of(data)
    if details and details not in text:
        text = f"{text}: {details}"
    return redact(text)[:300]


# ---------------------------------------------------------------------
# The session state machine


@dataclass
class _Pending:
    """A permission request waiting on us."""

    rpc_id: Any
    options: list = field(default_factory=list)  # {id, optionId, kind}


@dataclass
class _Tool:
    turn_id: str
    name: str = "tool"
    title: str = ""
    raw_input: Any = None
    content: Any = None
    announced: bool = False


class AcpState(SessionCore):
    """One ACP session's protocol state, from the adapter's side.

    The adapter calls ``start_events`` after the handshake, ``begin``
    once a ``session/prompt`` is written, ``update`` per
    ``session/update``, ``permission`` per
    ``session/request_permission``, ``answer`` per ``respond``,
    ``cancel_sent`` per ``interrupt``, ``prompt_answered`` with the
    prompt's answer, and ``exited`` or ``closed`` once at the end.

    ==========================================  ==========================
    Agent message                               v2 events
    ==========================================  ==========================
    ``agent_message_chunk``                     ``message.delta``; the
                                                message is completed when
                                                another kind, a tool call
                                                or the turn's end follows
    ``agent_thought_chunk``                     ``message.delta`` kind
                                                thinking, then
                                                ``thinking.completed``
    ``tool_call``, ``tool_call_update``         ``tool.started`` once the
                                                call has its input or
                                                runs; ``tool.completed``
                                                at ``completed`` or
                                                ``failed``
    ``plan``                                    ``plan.updated``
    ``usage_update``, a chunk's ``_meta.usage``  ``usage.updated``
    ``current_mode_update``                     ``session.options`` (the
                                                mode is kept)
    ``session/request_permission``              ``approval.requested``,
                                                ``session.state`` waiting
    the answer to ``session/prompt``            open streams completed,
                                                requests retired,
                                                ``turn.ended``,
                                                ``session.state`` idle
    anything else                               nothing (``unknown``)
    ==========================================  ==========================
    """

    def __init__(self, session: str, **kw: Any) -> None:
        super().__init__(session, **kw)
        self.mode = ""
        self.modes: list[str] = []
        self.models: dict[str, str] = {}
        # What ``session.options`` says (G32): the agent's model list and
        # its current model id.
        self.model = ""
        self.model_choices: list[dict] = []
        # the kind streaming now -> its stream id, and the agent's
        # messageId for it
        self._cur: dict[str, str] = {}
        self._mids: dict[str, str] = {}
        self._n_stream = 0
        self._tools: dict[str, _Tool] = {}
        self._n_request = 0
        self._usage: dict = {}
        self._cancelled = False

    # -- calls from the adapter ------------------------------------------

    def session_info(self, info: Any) -> None:
        """Keep what the session answer lists: modes and models."""
        self.modes = mode_ids(info)
        self.mode = current_mode(info)
        self.models = model_ids(info)
        self.model = current_model(info)
        self.model_choices = model_choices(info)

    def options(self) -> dict:
        """The agent's models and modes, each sent at once with
        ``session/set_model`` or ``session/set_mode``. A mode set while
        a prompt runs leaves its reply to finish (``reply_finishes``,
        plan_mode_mid). A model set then was taken and the reply went
        on, but the recording's agent lists one model, so no switch
        could be seen: ``unverified`` (model_mid)."""
        return {
            "model": opts.string_option(self.model, opts.UNVERIFIED,
                                        choices=self.model_choices),
            # The value only, never the agent's mode list: a page does
            # not offer a more permissive mode (G32, Limits).
            "permission_mode": opts.string_option(self.mode,
                                                  opts.REPLY_FINISHES),
            "plan_mode": opts.flag_option(self.mode == PLAN_MODE,
                                          opts.REPLY_FINISHES),
        }

    def mode_set(self, mode: str) -> list:
        """The agent took ``session/set_mode`` ``mode``."""
        self.mode = mode
        return self.options_events()

    def model_set(self, model_id: str) -> list:
        """The agent took ``session/set_model`` ``model_id``."""
        self.model = model_id
        return self.options_events()

    @property
    def busy(self) -> bool:
        return self.turn is not None

    def begin(self, text: str, *, attachments: Iterable[str] = (),
              command_id: str = "", device: str = "") -> tuple[str, list]:
        """The prompt is written: the turn is open."""
        self._cancelled = False
        return self.begin_turn(text, attachments=attachments,
                               command_id=command_id, device=device)

    def cancel_sent(self) -> tuple[list, list]:
        """``session/cancel`` is written: ``(replies, events)``. Every
        open permission request is answered ``cancelled`` (the
        protocol's rule) and retired; nothing more is granted in this
        turn."""
        if self.turn is None:
            return [], []
        self._cancelled = True
        replies = [(p.rpc_id, _CANCELLED) for p in self.pending.values()]
        return replies, self.interrupt_sent()

    def answer(self, request_id: str,
               answer: Answer) -> tuple[tuple, list]:
        """``((rpc id, result), events)`` for our answer to an open
        permission request. Raises UnknownRequest, or AdapterError
        ``bad_request`` when the request offers no such choice."""
        pending: _Pending = self.take(request_id)
        chosen = None
        if answer.option_id and answer.option_id not in _OPTION_KINDS.values():
            chosen = next((o for o in pending.options
                           if o["id"] == answer.option_id), None)
            if chosen is None:
                raise AdapterError("that option is not offered",
                                   code="bad_request")
        else:
            decision = answer.decision or answer.option_id or "deny"
            # A bare text answer is feedback: the call does not run. A
            # bare "always" is a one-time allow: an agent's standing
            # choices write rules into the project or the user's own
            # settings, and only naming one of them (``option_id``,
            # with the agent's label for it on the card) does that.
            want = "deny" if decision == "deny" else "allow"
            chosen = next((o for o in pending.options
                           if o["kind"] == want and not o["standing"]),
                          None)
            if chosen is None:
                raise AdapterError(
                    f"this request offers no {decision} option; answer "
                    "with one of its own", code="bad_request")
        result = {"outcome": {"outcome": "selected",
                              "optionId": chosen["optionId"]}}
        return (pending.rpc_id, result), self.answered(
            request_id, answer, chosen["kind"])

    # -- messages from the agent -----------------------------------------

    def update(self, params: Any) -> Fed:
        """One ``session/update`` notification's params."""
        fed = Fed()
        if self.done or not isinstance(params, dict):
            return fed
        if params.get("sessionId") not in (None, self.session_id):
            self.note_unknown("other-session")
            return fed
        update = params.get("update")
        what = update.get("sessionUpdate") if isinstance(update, dict) \
            else None
        handler = _UPDATES.get(what) if isinstance(what, str) else None
        if handler is None:
            self.note_unknown(what or "?")
            return fed
        handler(self, update, fed.events)
        self.sync_options(fed.events)
        return fed

    def _chunk(self, kind: str, update: dict, out: list) -> None:
        meta = update.get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get("usage"), dict):
            self._usage = meta["usage"]
        content = update.get("content")
        text = text_of(content.get("text")) if isinstance(content, dict) \
            and content.get("type") == "text" else ""
        if not text or self.turn is None:
            return
        mid = text_of(update.get("messageId"))
        # A chunk of another kind in between ends the message before.
        for other in list(self._cur):
            if other != kind:
                self._end_stream(out, other)
        if kind in self._cur and mid and mid != self._mids.get(kind):
            self._end_stream(out, kind)
        if kind not in self._cur:
            # Never the agent's messageId alone: an agent may reuse one
            # across turns, and a completed id never streams again.
            self._n_stream += 1
            self._cur[kind] = (f"{self.turn.turn_id}:"
                               f"{'r' if kind == 'thinking' else 'm'}"
                               f"{self._n_stream}")
            self._mids[kind] = mid
        self.delta(out, self._cur[kind], kind, text)

    def _end_stream(self, out: list, kind: str) -> None:
        sid = self._cur.pop(kind, "")
        self._mids.pop(kind, None)
        block = self.streams.get(sid)
        if block is not None:
            self.complete(out, sid, kind, block.raw)

    def _end_streams(self, out: list) -> None:
        for kind in list(self._cur):
            self._end_stream(out, kind)

    def _on_message(self, update: dict, out: list) -> None:
        self._chunk("assistant", update, out)

    def _on_thought(self, update: dict, out: list) -> None:
        self._chunk("thinking", update, out)

    def _on_tool(self, update: dict, out: list) -> None:
        call_id = text_of(update.get("toolCallId"))
        if not call_id:
            return
        tool = self._tools.get(call_id)
        if tool is None:
            if update.get("sessionUpdate") != "tool_call" \
                    and "status" not in update:
                return  # an update for a call we never saw start
            tool = self._tools[call_id] = _Tool(self.tid() or "")
            while len(self._tools) > _TOOLS_MAX:
                self._tools.pop(next(iter(self._tools)))
        if text_of(update.get("kind")):
            tool.name = update["kind"]
        if text_of(update.get("title")):
            tool.title = update["title"]
        if update.get("rawInput"):
            tool.raw_input = update["rawInput"]
        status = update.get("status") if isinstance(
            update.get("status"), str) else None
        # Announced once it has its input or is past ``pending``: agents
        # send the call first and what it runs in a later update.
        if tool.raw_input or status in ("in_progress", "completed",
                                        "failed"):
            self._announce(out, call_id, tool)
        if isinstance(update.get("content"), list) and update["content"]:
            # An update names only what changed: output that came
            # before the call ended is still its result.
            tool.content = update["content"]
        if status in _TOOL_STATUS and tool.announced:
            del self._tools[call_id]
            same_turn = bool(tool.turn_id) and tool.turn_id == self.tid()
            content = tool.content
            self.out(out, ev.tool_completed(
                self.key, call_id, _TOOL_STATUS[status],
                name=redact(tool.name),
                output=clip(redact(_content_text(content))) or None,
                file_edits=file_edits(content),
                turn_id=tool.turn_id if same_turn else None, ts=self.now()))

    def _announce(self, out: list, call_id: str, tool: _Tool) -> None:
        if tool.announced:
            return
        tool.announced = True
        self._end_streams(out)
        name = redact(tool.name)
        summary = redact(tool.title)[:200] or tool_summary(
            name, tool.raw_input)
        same_turn = bool(tool.turn_id) and tool.turn_id == self.tid()
        self.out(out, ev.tool_started(
            self.key, call_id, name, summary=summary,
            input=jsonable(redact(tool.raw_input))
            if tool.raw_input is not None else None,
            turn_id=tool.turn_id if same_turn else None, ts=self.now()))

    def _on_plan(self, update: dict, out: list) -> None:
        entries = update.get("entries")
        if not isinstance(entries, list):
            return
        items = []
        for entry in entries:
            if isinstance(entry, dict) and text_of(entry.get("content")):
                status = entry.get("status")
                items.append({
                    "text": redact(entry["content"]),
                    "status": status if isinstance(status, str)
                    and status in _PLAN_STATUSES else "pending"})
        # An empty list too: it is how an agent clears its plan.
        self.out(out, ev.plan_updated(self.key, items,
                                      turn_id=self.tid(), ts=self.now()))

    def _on_usage(self, update: dict, out: list) -> None:
        usage = self._usage
        counts = {name: count(value) for name, value in (
            ("input_tokens", usage.get("inputTokens")),
            ("output_tokens", usage.get("outputTokens")),
            ("cache_read_tokens", usage.get("cachedReadTokens")),
            ("context_tokens", update.get("used")),
            ("context_window", update.get("size")))}
        counts = {k: v for k, v in counts.items() if v is not None}
        if counts:
            self.out(out, ev.usage_updated(
                self.key, turn_id=self.tid(), ts=self.now(), **counts))

    def _on_mode(self, update: dict, out: list) -> None:
        # Always the agent's word, with no guard against late reports
        # (unlike opencode): updates and answers share one ordered
        # stream, and the keeper sends the next set only after the last
        # one's answer, so an update the agent writes while it handles a
        # set lands before the next set is even sent. ACP has this update
        # for the agent's own changes, and qwen sent none after a set.
        if text_of(update.get("currentModeId")):
            self.mode = update["currentModeId"]

    def permission(self, rpc_id: Any, params: Any) -> Fed:
        """A ``session/request_permission`` request. A request that
        cannot be shown, or that comes after the turn was cancelled, is
        answered ``cancelled`` (``Fed.replies``), so the agent never
        waits on us."""
        fed = Fed()
        params = params if isinstance(params, dict) else {}
        raw = params.get("options")
        options = [o for o in raw if isinstance(o, dict)
                   and text_of(o.get("optionId"))] \
            if isinstance(raw, list) else []
        if self.done or not options or self._cancelled or (
                params.get("sessionId") not in (None, self.session_id)):
            fed.replies.append((rpc_id, _CANCELLED))
            return fed
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        call_id = text_of(call.get("toolCallId"))
        tool = self._tools.get(call_id)
        if tool is not None:
            # What it asks to run is the call's input, if it came here.
            if call.get("rawInput"):
                tool.raw_input = call["rawInput"]
            if text_of(call.get("title")):
                tool.title = call["title"]
            self._announce(fed.events, call_id, tool)
        # Redacted before the prompt is built: it cuts what it quotes.
        known = {cid: {"title": redact(t.title),
                       "rawInput": redact(t.raw_input)}
                 for cid, t in self._tools.items()}
        prompt = wire._permission_prompt(redact(call), known)
        self._n_request += 1
        request_id = f"{self.run_id}-p{self._n_request}"
        shown, kept = [], []
        for n, option in enumerate(options, 1):
            native = option.get("kind") if isinstance(
                option.get("kind"), str) else ""
            kind = _OPTION_KINDS.get(native, "")
            row: dict[str, Any] = {"id": f"o{n}", "label": redact(text_of(
                option.get("name")) or option["optionId"])[:140]}
            if kind:
                row["kind"] = kind
            if native in wire._STANDING_KINDS:
                row["standing"] = True
            shown.append(row)
            kept.append({"id": f"o{n}", "optionId": option["optionId"],
                         "kind": kind, "standing": bool(row.get("standing"))})
        executes = call.get("kind") == "execute" or (
            tool is not None and tool.name == "execute")
        self.ask(fed.events, request_id,
                 "command" if executes else "tool", prompt,
                 _Pending(rpc_id, kept), options=shown, call_id=call_id)
        return fed

    def prompt_answered(self, msg: Any) -> list:
        """The answer to the turn's ``session/prompt``."""
        out: list = []
        turn = self.turn
        if self.done or turn is None or not isinstance(msg, dict):
            return out
        self._end_streams(out)
        if "error" in msg:
            reason = error_text(msg["error"])
            turn.error, turn.reason = True, reason
            self.out(out, ev.error(self.key, "agent_error", reason,
                                   turn_id=turn.turn_id, ts=self.now()))
            self.finish_turn(out, "failed", reason)
            return out
        result = msg.get("result")
        stop = text_of(result.get("stopReason")) \
            if isinstance(result, dict) else ""
        status = _STOP_STATUS.get(stop)
        if status is None:
            # max_tokens, max_turn_requests, refusal: the turn is over,
            # and the reason says why it stopped short.
            self.finish_turn(out, "completed", redact(stop) or None)
        else:
            self.finish_turn(out, status)
        return out

    def turn_cleared(self) -> None:
        self._cur.clear()
        self._mids.clear()
        self._cancelled = False
        for call_id in [c for c, t in self._tools.items()
                        if not t.announced]:
            del self._tools[call_id]


_UPDATES = {
    "agent_message_chunk": AcpState._on_message,
    "agent_thought_chunk": AcpState._on_thought,
    "tool_call": AcpState._on_tool,
    "tool_call_update": AcpState._on_tool,
    "plan": AcpState._on_plan,
    "usage_update": AcpState._on_usage,
    "current_mode_update": AcpState._on_mode,
}

# The updates ``AcpState`` reads: the recorder keeps these whole and
# cuts every other one down to its name.
READ_UPDATES = frozenset(_UPDATES)


# ---------------------------------------------------------------------
# Replaying a recorded fixture


def replay(lines: list[dict], *, session: str = FIXTURE_SESSION) -> list:
    """The v2 events a recorded fixture comes to, through the same
    :class:`AcpState` calls the adapter makes.

    A fixture line is ``{"proc", "t", "dir": "in" | "out", "msg"}`` (one
    JSON-RPC message) or ``{"proc", "exit": code}``. Timestamps are
    those on a fixed epoch."""
    header = lines[0] if lines and "fixture" in lines[0] else {}
    now = [0.0]

    def clock() -> float:
        return round(FIXTURE_T0 + now[0], 3)

    out: list = []
    states: dict[int, AcpState] = {}
    sent: dict[tuple, dict] = {}
    turns: dict[int, Any] = {}
    for line in lines[1:] if header else lines:
        proc = line.get("proc", 0)
        if isinstance(line.get("t"), (int, float)):
            now[0] = float(line["t"])
        state = states.get(proc)
        if state is None:
            state = states[proc] = AcpState(
                session, clock=clock, run_id=f"{proc + 1:08x}")
        if "exit" in line:
            out.extend(state.closed())
            continue
        msg = line.get("msg")
        if not isinstance(msg, dict):
            continue
        if line.get("dir") == "out":
            out.extend(_replay_out(state, proc, msg, sent, turns))
        else:
            out.extend(_replay_in(state, proc, msg, sent, turns))
    return out


def _replay_out(state: AcpState, proc: int, msg: dict, sent: dict,
                turns: dict) -> list:
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method and "id" in msg:
        sent[(proc, msg["id"])] = msg
    if method == "session/prompt" and state.started:
        blocks = params.get("prompt") or [{}]
        turns[proc] = msg["id"]
        attached = [urllib.parse.unquote(b["uri"][len("file://"):])
                    for b in blocks[1:] if isinstance(b, dict)
                    and b.get("type") == "resource_link"
                    and text_of(b.get("uri")).startswith("file://")]
        return state.begin(text_of(blocks[0].get("text")),
                           attachments=attached)[1]
    if method == "session/cancel":
        return state.cancel_sent()[1]
    if method is None and isinstance(msg.get("result"), dict):
        # Our answer to a permission request.
        option = (msg["result"].get("outcome") or {}).get("optionId")
        for request_id, pending in list(state.pending.items()):
            if pending.rpc_id != msg.get("id"):
                continue
            row = next((o for o in pending.options
                        if o["optionId"] == option), None)
            if row is not None:
                return state.answer(request_id,
                                    Answer(option_id=row["id"]))[1]
    return []


def _replay_in(state: AcpState, proc: int, msg: dict, sent: dict,
               turns: dict) -> list:
    method = msg.get("method")
    if method == "session/update":
        return state.update(msg.get("params")).events
    if method == "session/request_permission" and "id" in msg:
        return state.permission(msg["id"], msg.get("params")).events
    if method is not None or "id" not in msg:
        return []
    request = sent.pop((proc, msg["id"]), {})
    asked = request.get("method")
    result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
    if asked in ("session/new", "session/load") and "error" not in msg \
            and not state.started:
        state.session_info(result)
        session_id = text_of(result.get("sessionId")) or text_of(
            (request.get("params") or {}).get("sessionId"))
        caps = ALL_CAPS.narrowed(PROVEN).narrowed(advertised(result))
        return state.start_events(caps, session_id)
    if asked == "session/prompt" and turns.get(proc) == msg["id"]:
        turns.pop(proc, None)
        return state.prompt_answered(msg)
    params = request.get("params") or {}
    if "error" not in msg and asked == "session/set_mode" \
            and text_of(params.get("modeId")):
        return state.mode_set(params["modeId"])
    if "error" not in msg and asked == "session/set_model" \
            and text_of(params.get("modelId")):
        return state.model_set(params["modelId"])
    return []


# ---------------------------------------------------------------------
# The adapter


class _Agent(wire.Connection):
    """The wire's ``Connection`` with every message handed to the
    adapter instead of the hub's sinks, and its writes bounded."""

    def __init__(self, owner: "AcpAdapter", argv: list, cwd: str,
                 env: dict, spawn: Any, write_timeout: float) -> None:
        self._owner = owner
        self._reader_ident = 0
        self._writer: LineWriter | None = None
        self._write_timeout = write_timeout
        super().__init__(argv, cwd, spawn=_with_env(spawn, env))
        self._writer = LineWriter(self.proc.stdin, "acp-stdin")

    def _read_loop(self) -> None:
        self._reader_ident = threading.get_ident()
        super()._read_loop()

    def _write(self, payload: dict) -> bool:
        writer = self._writer
        if writer is None:  # a message before the constructor finished
            return super()._write(payload)
        # The reader's own answers are queued, never waited on: it must
        # keep reading even when the agent has stopped reading us.
        waits = threading.get_ident() != self._reader_ident
        try:
            return writer.send(json.dumps(payload).encode() + b"\n",
                               self._write_timeout if waits else None)
        except Stalled:
            # The agent stopped reading: end it, its exit says so.
            self.kill()
            return False

    def kill(self) -> None:
        try:
            self.proc.kill()
        except (OSError, AttributeError):
            pass

    def _update(self, params: dict) -> None:
        if not self.replaying:
            self._owner._on_update(params)

    def _permission(self, rpc_id: Any, params: dict) -> None:
        self._owner._on_permission(rpc_id, params)

    def _dispatch(self, msg: dict) -> None:
        turn = "method" not in msg and msg.get("id") is not None \
            and msg.get("id") == self.turn_rpc
        super()._dispatch(msg)
        if turn:
            self._owner._on_prompt_answer(msg)

    def _closed(self) -> None:
        self._owner._on_exit()
        super()._closed()

    def close(self, grace_s: float = 2.0) -> None:
        """EOF on stdin through the writer, then terminate, then kill."""
        self.dead = True
        writer = self._writer
        eof = False
        if writer is not None and not writer.stalled:
            try:
                eof = writer.send(None, min(max(grace_s, 0.1), 1.0))
            except Stalled:
                eof = False
        try:
            self.proc.wait(timeout=max(0.0, grace_s) if eof else 0)
        except subprocess.TimeoutExpired:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=_KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=_KILL_WAIT_S)
                except (OSError, subprocess.SubprocessError):
                    pass
            except OSError:
                pass
        if writer is not None:
            writer.stop(_KILL_WAIT_S)


def _with_env(spawn: Any, env: dict) -> Callable[..., Any]:
    """``Connection`` merges ``env`` over ``os.environ``; the adapter's
    environment is the whole of it."""
    popen = spawn or subprocess.Popen

    def start(argv: list, **kw: Any) -> Any:
        kw["env"] = env
        kw["stderr"] = subprocess.DEVNULL
        return popen(argv, **kw)

    return start


class AcpAdapter:
    """See the module docstring. ``resolved`` is the registry's
    :class:`~swe.remote.adapters.registry.Resolved` (None in tests that
    pass ``caps`` directly). ``spawn`` replaces ``subprocess.Popen``;
    ``clock`` the event timestamps; ``env`` the base environment
    (default ``os.environ``)."""

    adapter_id = "acp"

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
        self._caps = ceiling.narrowed(PROVEN)
        self.resolved = resolved
        self._argv = getattr(resolved, "argv", None)
        self._harness = getattr(resolved, "harness", "")
        self._min_version = getattr(resolved, "min_version", "")
        self._spawn = spawn
        self._clock = clock
        self._env = dict(os.environ if env is None else env)
        self._init_timeout = init_timeout
        self._call_timeout = call_timeout
        self._write_timeout = write_timeout
        self._run_id = run_id
        self._lock = threading.RLock()
        self._agent: _Agent | None = None
        self._sink: EventSink | None = None
        self._state: AcpState | None = None
        self._spec: StartSpec | None = None
        self._session_id = ""
        self._closed = False
        self._failed = False
        # The mode to go back to when plan mode is turned off.
        self._mode_before = ""
        self.version = ""
        self.agent_name = ""
        self.dropped: list = []

    # -- small helpers ---------------------------------------------------

    @property
    def capabilities(self) -> Caps:
        """The caps in force: harness.json's met with what the fixtures
        prove, then with what this agent advertised."""
        state = self._state
        return state.caps if state is not None and state.started \
            else self._caps

    @property
    def session_id(self) -> str:
        return self._session_id

    def _emit(self, events: list) -> None:
        sink = self._sink
        if sink is None:
            return
        for event in events:
            sink.emit(event)

    def _live(self) -> AcpState:
        if self._state is None or self._spec is None:
            raise AdapterError("the session is not started",
                               code="not_started")
        if self._state.done or self._closed:
            raise AdapterError("the agent is not running", code="agent_down")
        return self._state

    def _call(self, method: str, params: dict,
              timeout: float | None = None) -> dict:
        """One request and its answer, never under the lock (the reader
        needs it to get to the answer). AcpError becomes AdapterError:
        ``timeout``, ``agent_down`` or ``refused``."""
        agent = self._agent
        assert agent is not None
        try:
            return agent.call(method, params,
                              timeout=self._call_timeout
                              if timeout is None else timeout)
        except wire.AcpError as exc:
            raise _adapter_error(exc) from None

    # -- SessionAdapter --------------------------------------------------

    def start(self, spec: StartSpec, sink: EventSink) -> str:
        if self._closed:
            raise AdapterError("the adapter is closed", code="closed")
        if self._spec is not None or self._failed:
            raise AdapterError("already started", code="started")
        if spec.fork:
            self._caps.require("fork")
        argv = list(spec.argv or self._argv
                    or wire.command_for(self._harness or spec.harness) or ())
        if not argv:
            self._failed = True
            raise AdapterError(
                f"{self._harness or spec.harness or 'this harness'} has no "
                "ACP command on this machine", code="agent_down")
        self._spec = spec
        self._sink = sink
        self._state = AcpState(spec.session_key, clock=self._clock,
                               caps=self._caps, run_id=self._run_id)
        try:
            self._agent = _Agent(self, argv, spec.cwd,
                                 {**self._env, **dict(spec.env)},
                                 self._spawn, self._write_timeout)
        except (OSError, ValueError) as exc:
            self._failed = True
            raise AdapterError(f"the agent did not start: {exc}",
                               code="agent_down") from None
        try:
            self._handshake(spec)
        except AdapterError:
            self._abort()
            raise
        return self._session_id

    def _handshake(self, spec: StartSpec) -> None:
        agent, state = self._agent, self._state
        assert agent is not None and state is not None
        deadline = time.monotonic() + self._init_timeout

        def left() -> float:
            # One budget for the whole start, the options included.
            return max(0.1, deadline - time.monotonic())

        try:
            agent.handshake(spec.resume_id, allow_new=not spec.resume_id,
                            budget=self._init_timeout)
        except wire.NoLoadSession as exc:
            raise AdapterError(redact(str(exc)), code="bad_request") from None
        except wire.AcpError as exc:
            raise _adapter_error(exc) from None
        self.agent_name = agent.label
        self.version = agent.agent_version
        if self.version and self._min_version and not version_ok(
                self.version, self._min_version):
            raise AdapterError(
                f"{agent.label} {self.version} is older than "
                f"{self._min_version}, the oldest this adapter drives; "
                "update it", code="unsupported_version")
        info = agent.session_info
        state.session_info(info)
        caps = self._caps.narrowed(advertised(info))
        # What the launch asked for, where this agent can do it. A
        # default meant for another harness never breaks a start.
        if spec.model and caps.can_set("model") \
                and spec.model in state.models:
            self._set_model(spec.model, left())
        mode = spec.permission_mode if caps.can_set("permission_mode") \
            and spec.permission_mode in state.modes else ""
        if spec.plan_mode and caps.plan_mode:
            self._set_mode(PLAN_MODE, left(), plan=True)
            if mode and mode != PLAN_MODE:
                # The mode to go back to is the one the launch (or the
                # keeper's restart) asked for, not the agent's default.
                self._mode_before = mode
        elif mode:
            self._set_mode(mode, left())
        with self._lock:
            if state.done:
                raise AdapterError("the agent exited during the handshake",
                                   code="agent_down")
            self._session_id = agent.session_id
            # Last: a start that fails above has emitted nothing.
            self._emit(state.start_events(caps, self._session_id))

    def _abort(self) -> None:
        """A start that failed: stop the child, emit nothing."""
        self._failed = True
        with self._lock:
            if self._state is not None:
                self._state.done = True
        if self._agent is not None:
            self._agent.close(0.5)

    def send_turn(self, turn: TurnInput) -> str:
        with self._lock:
            state = self._live()
            agent = self._agent
            assert agent is not None
            if state.busy:
                raise AdapterError("a turn is already running", code="busy")
            # Written under the lock: the reader cannot take the turn's
            # first update before the turn is open.
            try:
                agent.prompt(turn.text, resource_links(turn.attachments))
            except wire.AcpError as exc:
                if "already running" in str(exc):
                    raise AdapterError("a turn is already running",
                                       code="busy") from None
                raise AdapterError("the agent is not reachable",
                                   code="agent_down") from None
            turn_id, events = state.begin(
                turn.text, attachments=turn.attachments,
                command_id=turn.command_id, device=turn.device)
            self._emit(events)
            return turn_id

    def steer(self, turn: TurnInput) -> None:
        with self._lock:
            state = self._live()
            self.capabilities.require("steer")
            if state.turn is None:  # pragma: no cover - PROVEN keeps it off
                raise AdapterError("no turn to steer", code="idle")
        raise Unsupported("steer")  # pragma: no cover

    def interrupt(self) -> None:
        with self._lock:
            state = self._live()
            agent = self._agent
            assert agent is not None
            if state.turn is None:
                return
            if not agent.notify("session/cancel",
                                {"sessionId": self._session_id}):
                raise AdapterError("the agent is not reachable",
                                   code="agent_down")
            replies, events = state.cancel_sent()
            for rpc_id, result in replies:
                agent._respond(rpc_id, result)
            self._emit(events)

    def respond(self, request_id: str, answer: Answer) -> None:
        with self._lock:
            state = self._live()
            agent = self._agent
            assert agent is not None
            pending = state.pending.get(request_id)
            (rpc_id, result), events = state.answer(request_id, answer)
            if not agent._respond(rpc_id, result):
                # Still open: the exit sequence retires it.
                state.reopen(request_id, pending)
                raise AdapterError("the agent is not reachable",
                                   code="agent_down")
            self._emit(events)

    def set_option(self, name: str, value: Any) -> None:
        with self._lock:
            state = self._live()
            check_option(self.capabilities, name, value)
            if name == "model" and value not in state.models:
                raise AdapterError(
                    f"{value!r} is not one of this agent's models "
                    f"({', '.join(sorted(state.models)) or 'none listed'})",
                    code="bad_request")
            if name == "permission_mode" and value not in state.modes:
                raise AdapterError(
                    f"{value!r} is not one of this agent's modes "
                    f"({', '.join(state.modes)})", code="bad_request")
            mode_now = state.mode
        if name == "model":
            self._set_model(value)
        elif name == "permission_mode":
            self._set_mode(value)
        elif value:
            self._set_mode(PLAN_MODE, plan=True)
        else:
            if mode_now != PLAN_MODE:
                return
            # Back to the mode from before plan mode, else the agent's
            # ``default``; never the first mode it lists, which could be
            # one that asks no approvals at all (G32).
            back = self._mode_before if self._mode_before in state.modes \
                and self._mode_before != PLAN_MODE else (
                    FALLBACK_MODE if FALLBACK_MODE in state.modes else "")
            if not back:
                raise AdapterError(
                    "plan mode stays on: there is no mode from before it to "
                    f"go back to, and this agent lists no {FALLBACK_MODE!r} "
                    "mode", code="refused")
            self._set_mode(back)

    def _set_mode(self, mode: str, timeout: float | None = None,
                  plan: bool = False) -> None:
        state = self._state
        assert state is not None
        before = state.mode
        # Sent even when we believe the mode is already this one: an
        # agent can leave a mode by itself without telling us.
        self._call("session/set_mode", {"sessionId": self._agent.session_id,
                                        "modeId": mode}, timeout)
        with self._lock:
            if plan and before and before != PLAN_MODE:
                self._mode_before = before
            self._emit(state.mode_set(mode))

    def _set_model(self, model: str, timeout: float | None = None) -> None:
        state = self._state
        assert state is not None
        model_id = state.models[model]
        self._call("session/set_model", {
            "sessionId": self._agent.session_id,
            "modelId": model_id}, timeout)
        with self._lock:
            self._emit(state.model_set(model_id))

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            self._live()
        self.capabilities.require("stop_task")
        raise Unsupported("stop_task")  # pragma: no cover - PROVEN keeps it off

    def rollback(self, turn_id: str) -> None:
        with self._lock:
            self._live()
        self.capabilities.require("rollback")
        raise Unsupported("rollback")  # pragma: no cover - PROVEN keeps it off

    def transcript_position(self) -> TranscriptPosition | None:
        """None: each agent keeps its sessions its own way."""
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
        if self._agent is not None:
            self._agent.close(grace_s)

    # -- from the reader thread ------------------------------------------

    def _fed(self, fed: Fed) -> None:
        agent = self._agent
        for rpc_id, result in fed.replies:
            if agent is not None:
                agent._respond(rpc_id, result)
        self._emit(fed.events)

    def _on_update(self, params: dict) -> None:
        with self._lock:
            state = self._state
            if state is not None and state.started:
                self._fed(state.update(params))

    def _on_permission(self, rpc_id: Any, params: dict) -> None:
        with self._lock:
            state = self._state
            if state is None or not state.started:
                if self._agent is not None:
                    self._agent._respond(rpc_id, _CANCELLED)
                return
            self._fed(state.permission(rpc_id, params))

    def _on_prompt_answer(self, msg: dict) -> None:
        with self._lock:
            state = self._state
            if state is not None and state.started:
                self._emit(state.prompt_answered(msg))

    def _on_exit(self) -> None:
        agent = self._agent
        code = None
        if agent is not None:
            try:
                code = agent.proc.wait(timeout=_KILL_WAIT_S)
            except (subprocess.TimeoutExpired, OSError, AttributeError):
                code = None
        with self._lock:
            state = self._state
            if state is None or state.done or self._closed or self._failed:
                return
            if not state.started:
                # Gone before the handshake: start() reports it.
                state.done = True
                return
            label = self.agent_name or "the agent"
            self._emit(state.exited(f"{label} exited" + (
                f" with code {code}" if code not in (None, 0) else "")))


def _adapter_error(exc: Exception) -> AdapterError:
    text = redact(str(exc))[:300]
    if "timed out" in text or "took too long" in text:
        return AdapterError(text, code="timeout")
    if "exited" in text or "not reachable" in text:
        return AdapterError(text, code="agent_down")
    return AdapterError(text, code="refused")


registry.register(AcpAdapter.adapter_id, AcpAdapter)
