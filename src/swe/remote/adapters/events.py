"""Canonical v2 session events: what every adapter emits.

Envelope::

    {"v": 2, "type": "tool.completed", "session": "claude:abc",
     "seq": 41, "ts": 1760000000.0, "turn_id": "t3",
     "command_id": "c-9f2", "data": {...}}

``seq`` is 0 when an adapter builds the event; the keeper's log stamps
the real one. ``message.delta`` is transient (never logged, never
replayed) and carries no ``seq`` at all, exactly like v1's delta, so it
can never move a client's cursor. ``turn_id`` and ``command_id`` are
present only when they apply.

Field names follow v1 (:mod:`swe.remote.protocol`) where the meaning is
the same: ``request_id``, ``prompt``, ``options``, ``decision`` and
``source`` on approvals; ``kind``, ``text`` on messages; ``id``,
``kind``, ``text``, ``seq``, ``done`` on a delta; ``call_id`` on tools;
``duration_s`` on thinking; ``op``, ``ok``, ``note`` on a command result.

Every type has a builder below; each builds through :func:`make`, which
raises ValueError on an event :func:`validate` would reject, so an
adapter cannot emit a malformed one.
"""

from __future__ import annotations

import json
import time
from typing import Any

from swe.remote.protocol import NOTIFY_KINDS

VERSION = 2

# ---------------------------------------------------------------------
# Enum values

SESSION_STATES = frozenset({
    "starting",    # the agent is being started or resumed
    "idle",        # ready for a turn
    "running",     # a turn is in flight
    "waiting",     # a turn is blocked on the user (approval or input)
    "agent_down",  # the agent exited on its own; the next send restarts it
    "parked",      # stopped by the keeper while idle; resumes on demand
    "closed",      # ended by close()
})
TURN_STATUSES = frozenset({"completed", "interrupted", "failed",
                           "keeper_lost"})
MESSAGE_KINDS = frozenset({"user", "assistant", "system"})
DELTA_KINDS = frozenset({"assistant", "thinking", "tool_use"})
TOOL_STATUSES = frozenset({"ok", "error", "cancelled"})
FILE_EDIT_KINDS = frozenset({"add", "modify", "delete"})
APPROVAL_KINDS = frozenset({"tool", "command", "question", "plan"})
DECISIONS = frozenset({"allow", "deny", "always"})
APPROVAL_SOURCES = frozenset({
    "user",        # a device answered (``by`` names it)
    "autonomy",    # answered by itself under a yolo launch
    "harness",     # answered in the harness's own UI
    "turn_ended",  # the turn ended or was interrupted first
    "agent_exit",  # the agent exited with the request open
    "closed",      # the session was closed with the request open
    "superseded",  # a newer request replaced it
    "expired",     # nobody answered in time
})
INPUT_KINDS = frozenset({"text", "choice"})
PLAN_ITEM_STATUSES = frozenset({"pending", "in_progress", "completed"})
TASK_KINDS = frozenset({"subagent", "background"})
TASK_STATUSES = frozenset({"running", "completed", "failed", "stopped"})
QUEUE_MODES = frozenset({"queue", "steer"})
RECEIPT_STATUSES = frozenset({"accepted", "duplicate", "queued",
                              "rejected", "held"})
# What a change of option does to a turn in progress (G32), each from a
# recording: ``next_turn`` leaves the whole turn alone (the adapter sends
# the change with the next turn); ``reply_finishes`` is sent at once,
# and the reply being written runs on to its end unchanged, the change
# taking hold after it; ``unverified`` is sent at once and nothing
# recorded shows what it does to the reply in progress.
MID_TURN = frozenset({"next_turn", "reply_finishes", "unverified"})
# What a steer does to the turn in progress: ``base.STEER_EFFECTS``
# (events.py imports nothing from base, so the values are spelled here
# too; a test holds the two equal).
STEER_EFFECTS = frozenset({"stops_reply", "after_tool", "next_turn"})
# The options ``session.options`` can carry, and which take a flag.
OPTION_NAMES = ("model", "effort", "permission_mode", "plan_mode")
FLAG_OPTIONS = frozenset({"plan_mode"})

# Every enum field, ``(event type, field) -> allowed values``, for
# clients and the JSON schema G9 generates. Nested fields use a dotted
# path (``options[].kind`` is free text, so it is not listed).
ENUMS: dict[tuple[str, str], frozenset] = {
    ("session.state", "state"): SESSION_STATES,
    ("turn.ended", "status"): TURN_STATUSES,
    ("message.delta", "kind"): DELTA_KINDS,
    ("message.completed", "kind"): MESSAGE_KINDS,
    ("tool.completed", "status"): TOOL_STATUSES,
    ("tool.completed", "file_edits[].kind"): FILE_EDIT_KINDS,
    ("approval.requested", "kind"): APPROVAL_KINDS,
    ("approval.resolved", "decision"): DECISIONS,
    ("approval.resolved", "source"): APPROVAL_SOURCES,
    ("input.requested", "kind"): INPUT_KINDS,
    ("plan.updated", "items[].status"): PLAN_ITEM_STATUSES,
    ("task.updated", "kind"): TASK_KINDS,
    ("task.updated", "status"): TASK_STATUSES,
    ("changes.updated", "files[].kind"): FILE_EDIT_KINDS,
    ("queue.updated", "items[].mode"): QUEUE_MODES,
    ("command.receipt", "status"): RECEIPT_STATUSES,
    ("notify", "kind"): NOTIFY_KINDS,
    ("session.caps", "caps.steer_effect"): STEER_EFFECTS,
    ("session.options", "options.model.mid_turn"): MID_TURN,
    ("session.options", "options.effort.mid_turn"): MID_TURN,
    ("session.options", "options.permission_mode.mid_turn"): MID_TURN,
    ("session.options", "options.plan_mode.mid_turn"): MID_TURN,
}


# ---------------------------------------------------------------------
# A small schema language: each field spec checks one value.


class _Spec:
    def check(self, value: Any, path: str, problems: list[str]) -> None:
        raise NotImplementedError  # pragma: no cover


class _Str(_Spec):
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty

    def check(self, value, path, problems):
        if not isinstance(value, str):
            problems.append(f"{path} must be a string")
        elif not self.empty and not value:
            problems.append(f"{path} must not be empty")


class _Int(_Spec):
    def __init__(self, *, least: int = 0) -> None:
        self.least = least

    def check(self, value, path, problems):
        if not isinstance(value, int) or isinstance(value, bool) \
                or value < self.least:
            what = "positive" if self.least == 1 else "non-negative"
            problems.append(f"{path} must be a {what} integer")


class _Num(_Spec):
    def check(self, value, path, problems):
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or value < 0:
            problems.append(f"{path} must be a non-negative number")


class _Bool(_Spec):
    def check(self, value, path, problems):
        if not isinstance(value, bool):
            problems.append(f"{path} must be true or false")


class _Enum(_Spec):
    def __init__(self, values: frozenset) -> None:
        self.values = values

    def check(self, value, path, problems):
        if not isinstance(value, str) or value not in self.values:
            problems.append(f"{path} must be one of {sorted(self.values)}")


class _Any(_Spec):
    def check(self, value, path, problems):
        return


class _Obj(_Spec):
    """A dict with required and optional fields; unknown keys are
    problems, so a typo in an adapter fails its tests."""

    def __init__(self, required: dict | None = None,
                 optional: dict | None = None, *, open_: bool = False) -> None:
        self.required = required or {}
        self.optional = optional or {}
        self.open = open_

    def check(self, value, path, problems):
        if not isinstance(value, dict):
            problems.append(f"{path} must be an object")
            return
        for name, spec in self.required.items():
            if name not in value:
                problems.append(f"{path}.{name} is required")
            else:
                spec.check(value[name], f"{path}.{name}", problems)
        for name, spec in self.optional.items():
            if name in value and value[name] is not None:
                spec.check(value[name], f"{path}.{name}", problems)
        if not self.open:
            for name in value:
                if name not in self.required and name not in self.optional:
                    problems.append(f"{path}.{name} is not a known field")


class _List(_Spec):
    def __init__(self, item: _Spec) -> None:
        self.item = item

    def check(self, value, path, problems):
        if not isinstance(value, list):
            problems.append(f"{path} must be a list")
            return
        for i, item in enumerate(value):
            self.item.check(item, f"{path}[{i}]", problems)


STR = _Str()
TEXT = _Str(empty=True)
INT = _Int()
COUNT = _Int(least=1)  # a count that starts at 1
NUM = _Num()
BOOL = _Bool()
ANY = _Any()
STRS = _List(STR)

_FILE_EDIT = _Obj({"path": STR, "kind": _Enum(FILE_EDIT_KINDS)},
                  {"diff": TEXT, "old_path": STR})
_OPTION = _Obj({"id": STR, "label": TEXT},
               {"kind": STR, "standing": BOOL})
_PLAN_ITEM = _Obj({"text": TEXT, "status": _Enum(PLAN_ITEM_STATUSES)})
_CHANGED_FILE = _Obj({"path": STR, "kind": _Enum(FILE_EDIT_KINDS)},
                     {"added": INT, "removed": INT})
_QUEUED = _Obj({"command_id": STR, "text": TEXT},
               {"by": STR, "mode": _Enum(QUEUE_MODES), "attachments": STRS})
# One value a ``session.options`` entry offers: the agent's own id, and
# its own name and description where it gives them.
_CHOICE = _Obj({"id": STR}, {"label": TEXT, "description": TEXT})
# An option whose value is a string. ``value`` is the one in force (""
# when the agent has not said); ``choices`` is there only when the agent
# listed them; ``reported`` is the agent's own spelling of ``value``
# when it differs (Claude answers ``haiku`` with ``claude-haiku-5-5``).
_STRING_OPTION = _Obj({"value": TEXT, "mid_turn": _Enum(MID_TURN)},
                      {"choices": _List(_CHOICE), "reported": STR})
_FLAG_OPTION = _Obj({"value": BOOL, "mid_turn": _Enum(MID_TURN)})
_OPTIONS = _Obj({}, {name: (_FLAG_OPTION if name in FLAG_OPTIONS
                            else _STRING_OPTION)
                     for name in OPTION_NAMES})
_CAPS = _Obj({"steer": BOOL, "plan_mode": BOOL, "rollback": BOOL,
              "fork": BOOL, "stop_task": BOOL, "options": STRS},
             {"steer_effect": _Enum(STEER_EFFECTS)})

# type -> the ``data`` object's schema.
SCHEMAS: dict[str, _Obj] = {
    "session.state": _Obj({"state": _Enum(SESSION_STATES)},
                          {"reason": TEXT, "harness_session_id": STR}),
    "session.caps": _Obj({"caps": _CAPS}),
    "session.options": _Obj({"options": _OPTIONS}),
    "turn.started": _Obj({}, {"by": STR, "text": TEXT}),
    "turn.ended": _Obj({"status": _Enum(TURN_STATUSES)},
                       {"reason": TEXT, "duration_s": NUM}),
    "message.delta": _Obj({"id": STR, "kind": _Enum(DELTA_KINDS),
                           "text": TEXT, "seq": COUNT},
                          {"offset": INT, "done": BOOL}),
    "message.completed": _Obj({"id": STR, "kind": _Enum(MESSAGE_KINDS),
                               "text": TEXT},
                              {"by": STR, "attachments": STRS}),
    "thinking.completed": _Obj({"id": STR, "text": TEXT},
                               {"duration_s": NUM}),
    "tool.started": _Obj({"call_id": STR, "name": STR},
                         {"summary": TEXT, "input": ANY, "task_id": STR}),
    "tool.updated": _Obj({"call_id": STR},
                         {"text": TEXT, "summary": TEXT}),
    "tool.completed": _Obj({"call_id": STR, "status": _Enum(TOOL_STATUSES)},
                           {"name": STR, "output": TEXT,
                            "file_edits": _List(_FILE_EDIT)}),
    "approval.requested": _Obj({"request_id": STR,
                                "kind": _Enum(APPROVAL_KINDS),
                                "prompt": TEXT},
                               {"options": _List(_OPTION), "call_id": STR,
                                "auto": BOOL}),
    "approval.resolved": _Obj({"request_id": STR,
                               "source": _Enum(APPROVAL_SOURCES)},
                              {"decision": _Enum(DECISIONS),
                               "option_id": STR, "text": TEXT,
                               "by": STR}),
    "input.requested": _Obj({"request_id": STR, "prompt": TEXT,
                             "kind": _Enum(INPUT_KINDS)},
                            {"options": _List(_OPTION)}),
    "plan.updated": _Obj({"items": _List(_PLAN_ITEM)}, {"text": TEXT}),
    "task.updated": _Obj({"task_id": STR, "kind": _Enum(TASK_KINDS),
                          "status": _Enum(TASK_STATUSES)},
                         {"parent_turn_id": STR, "title": TEXT,
                          "can_stop": BOOL}),
    "checkpoint.created": _Obj({"checkpoint_id": STR},
                               {"files": INT, "note": TEXT}),
    "checkpoint.restored": _Obj({"checkpoint_id": STR},
                                {"files": INT, "by": STR}),
    "changes.updated": _Obj({"files": _List(_CHANGED_FILE)}),
    "queue.updated": _Obj({"items": _List(_QUEUED)}),
    "command.receipt": _Obj({"status": _Enum(RECEIPT_STATUSES)},
                            {"reason": TEXT, "op": STR}),
    "command.result": _Obj({"op": STR, "ok": BOOL},
                           {"note": TEXT, "degraded": BOOL,
                            "cancelled": BOOL, "request_id": STR,
                            "launch_id": STR}),
    "usage.updated": _Obj({}, {"input_tokens": INT, "output_tokens": INT,
                               "cache_read_tokens": INT,
                               "cache_write_tokens": INT,
                               "context_tokens": INT,
                               "context_window": INT, "cost_usd": NUM}),
    "context.compacted": _Obj({}, {"summary": TEXT, "tokens_before": INT}),
    "error": _Obj({"code": STR, "message": TEXT}, {"fatal": BOOL}),
    "notify": _Obj({"kind": _Enum(NOTIFY_KINDS), "message": TEXT},
                   {"status": STR}),
}

EVENT_TYPES = frozenset(SCHEMAS)

# Everything but live text is logged and replayed.
TRANSIENT_TYPES = frozenset({"message.delta"})
DURABLE_TYPES = EVENT_TYPES - TRANSIENT_TYPES

# Envelope fields each type must carry beyond the common ones.
_NEEDS_TURN = frozenset({"turn.started", "turn.ended"})
_NEEDS_COMMAND = frozenset({"command.receipt", "command.result"})

_ENVELOPE_KEYS = frozenset({"v", "type", "session", "seq", "ts", "turn_id",
                            "command_id", "data"})


def _json_problem(event: dict) -> str:
    """Why ``event`` will not go to disk or a socket as strict JSON (a
    set, bytes, a tuple key, NaN or infinity anywhere in it), or ""."""
    try:
        json.dumps(event, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        return f"event is not encodable as JSON: {exc}"
    return ""


def validate(event: Any) -> list[str]:
    """Every problem with ``event`` as a v2 envelope; empty when valid.
    That includes anything ``json.dumps(event, allow_nan=False)`` cannot
    encode: the log and the wire are strict JSON."""
    if not isinstance(event, dict):
        return ["event must be an object"]
    problems: list[str] = []
    unencodable = _json_problem(event)
    if unencodable:
        problems.append(unencodable)
    if event.get("v") != VERSION:
        problems.append(f"v must be {VERSION}")
    etype = event.get("type")
    if not isinstance(etype, str) or etype not in SCHEMAS:
        problems.append(f"unknown event type {etype!r}")
        etype = None
    STR.check(event.get("session"), "session", problems)
    NUM.check(event.get("ts"), "ts", problems)
    if etype in TRANSIENT_TYPES:
        if "seq" in event:
            problems.append(f"{etype} is transient and carries no seq")
    else:
        INT.check(event.get("seq"), "seq", problems)
    for name in ("turn_id", "command_id"):
        if name in event and event[name] is not None:
            STR.check(event[name], name, problems)
    if etype in _NEEDS_TURN and not event.get("turn_id"):
        problems.append(f"{etype} needs a turn_id")
    if etype in _NEEDS_COMMAND and not event.get("command_id"):
        problems.append(f"{etype} needs a command_id")
    for name in event:
        if name not in _ENVELOPE_KEYS:
            problems.append(f"{name} is not an envelope field")
    if etype is not None:
        SCHEMAS[etype].check(event.get("data"), "data", problems)
    elif not isinstance(event.get("data"), dict):
        problems.append("data must be an object")
    return problems


def is_durable(event: dict) -> bool:
    """Whether the keeper logs ``event`` (everything but live text)."""
    return event.get("type") in DURABLE_TYPES


def make(
    etype: str,
    session: str,
    data: dict,
    *,
    turn_id: str | None = None,
    command_id: str | None = None,
    ts: float | None = None,
    seq: int = 0,
) -> dict:
    """Build and validate one v2 envelope. ``data`` entries that are
    None are dropped (an optional field left out). Raises ValueError
    listing every problem."""
    ev: dict[str, Any] = {
        "v": VERSION,
        "type": etype,
        "session": session,
        "seq": seq,
        "ts": ts if ts is not None else time.time(),
    }
    if etype in TRANSIENT_TYPES:
        del ev["seq"]
    if turn_id:
        ev["turn_id"] = turn_id
    if command_id:
        ev["command_id"] = command_id
    ev["data"] = {k: v for k, v in data.items() if v is not None}
    problems = validate(ev)
    if problems:
        raise ValueError(f"bad {etype} event: " + "; ".join(problems))
    return ev


# ---------------------------------------------------------------------
# Builders, one per type. Keyword-only past the session and the
# fields every event of that type needs.


def session_state(session: str, state: str, *, reason: str | None = None,
                  harness_session_id: str | None = None,
                  ts: float | None = None) -> dict:
    return make("session.state", session,
                {"state": state, "reason": reason,
                 "harness_session_id": harness_session_id}, ts=ts)


def session_caps(session: str, caps: dict, *, ts: float | None = None) -> dict:
    """``caps`` is ``base.Caps.as_dict()``."""
    return make("session.caps", session, {"caps": caps}, ts=ts)


def session_options(session: str, options: dict, *,
                    ts: float | None = None) -> dict:
    """The options a session offers now, each with the value in force:
    ``{name: {value, mid_turn, choices?, reported?}}`` for the
    ``set_option`` names its caps allow (G32). Emitted at start and
    whenever one of them changes, never on a timer."""
    return make("session.options", session, {"options": options}, ts=ts)


def turn_started(session: str, turn_id: str, *, by: str | None = None,
                 text: str | None = None, command_id: str | None = None,
                 ts: float | None = None) -> dict:
    return make("turn.started", session, {"by": by, "text": text},
                turn_id=turn_id, command_id=command_id, ts=ts)


def turn_ended(session: str, turn_id: str, status: str, *,
               reason: str | None = None, duration_s: float | None = None,
               command_id: str | None = None,
               ts: float | None = None) -> dict:
    return make("turn.ended", session,
                {"status": status, "reason": reason,
                 "duration_s": duration_s},
                turn_id=turn_id, command_id=command_id, ts=ts)


def message_delta(session: str, msg_id: str, kind: str, text: str,
                  seq: int, *, offset: int | None = None,
                  done: bool = False, turn_id: str | None = None,
                  ts: float | None = None) -> dict:
    """Live text. ``seq`` counts this message's deltas from 1 (v1's
    ``data.seq``); ``offset`` is where ``text`` starts in the message,
    so a client resuming mid-reply can join the stream."""
    return make("message.delta", session,
                {"id": msg_id, "kind": kind, "text": text, "seq": seq,
                 "offset": offset, "done": done},
                turn_id=turn_id, ts=ts)


def message_completed(session: str, msg_id: str, kind: str, text: str, *,
                      by: str | None = None,
                      attachments: list | None = None,
                      turn_id: str | None = None,
                      command_id: str | None = None,
                      ts: float | None = None) -> dict:
    return make("message.completed", session,
                {"id": msg_id, "kind": kind, "text": text, "by": by,
                 "attachments": attachments},
                turn_id=turn_id, command_id=command_id, ts=ts)


def thinking_completed(session: str, msg_id: str, text: str, *,
                       duration_s: float | None = None,
                       turn_id: str | None = None,
                       ts: float | None = None) -> dict:
    return make("thinking.completed", session,
                {"id": msg_id, "text": text, "duration_s": duration_s},
                turn_id=turn_id, ts=ts)


def tool_started(session: str, call_id: str, name: str, *,
                 summary: str | None = None, input: Any = None,
                 task_id: str | None = None, turn_id: str | None = None,
                 ts: float | None = None) -> dict:
    """``summary`` is the one-line card text (``Bash · ls -la``);
    ``input`` the tool's arguments, already redacted."""
    return make("tool.started", session,
                {"call_id": call_id, "name": name, "summary": summary,
                 "input": input, "task_id": task_id},
                turn_id=turn_id, ts=ts)


def tool_updated(session: str, call_id: str, *, text: str | None = None,
                 summary: str | None = None, turn_id: str | None = None,
                 ts: float | None = None) -> dict:
    return make("tool.updated", session,
                {"call_id": call_id, "text": text, "summary": summary},
                turn_id=turn_id, ts=ts)


def tool_completed(session: str, call_id: str, status: str, *,
                   name: str | None = None, output: str | None = None,
                   file_edits: list | None = None,
                   turn_id: str | None = None,
                   ts: float | None = None) -> dict:
    """``file_edits`` is ``[{path, kind, diff}]`` (``kind`` add, modify
    or delete; ``diff`` a unified diff)."""
    return make("tool.completed", session,
                {"call_id": call_id, "status": status, "name": name,
                 "output": output, "file_edits": file_edits},
                turn_id=turn_id, ts=ts)


def approval_requested(session: str, request_id: str, kind: str,
                       prompt: str, *, options: list | None = None,
                       call_id: str | None = None, auto: bool | None = None,
                       turn_id: str | None = None,
                       ts: float | None = None) -> dict:
    """``options`` is ``[{id, label, kind?, standing?}]``: the harness's
    own choices, in its order. ``auto`` marks one Autonomy answered by
    itself (no push, no inbox wait), as v1 does."""
    return make("approval.requested", session,
                {"request_id": request_id, "kind": kind, "prompt": prompt,
                 "options": options, "call_id": call_id, "auto": auto},
                turn_id=turn_id, ts=ts)


def approval_resolved(session: str, request_id: str, source: str, *,
                      decision: str | None = None,
                      option_id: str | None = None,
                      text: str | None = None, by: str | None = None,
                      turn_id: str | None = None,
                      command_id: str | None = None,
                      ts: float | None = None) -> dict:
    """Closes an ``approval.requested`` or an ``input.requested``."""
    return make("approval.resolved", session,
                {"request_id": request_id, "source": source,
                 "decision": decision, "option_id": option_id,
                 "text": text, "by": by},
                turn_id=turn_id, command_id=command_id, ts=ts)


def input_requested(session: str, request_id: str, prompt: str, *,
                    kind: str = "text", options: list | None = None,
                    turn_id: str | None = None,
                    ts: float | None = None) -> dict:
    return make("input.requested", session,
                {"request_id": request_id, "prompt": prompt, "kind": kind,
                 "options": options},
                turn_id=turn_id, ts=ts)


def plan_updated(session: str, items: list, *, text: str | None = None,
                 turn_id: str | None = None, ts: float | None = None) -> dict:
    """``items`` is ``[{text, status}]``; ``text`` the plan as written."""
    return make("plan.updated", session, {"items": items, "text": text},
                turn_id=turn_id, ts=ts)


def task_updated(session: str, task_id: str, kind: str, status: str, *,
                 parent_turn_id: str | None = None,
                 title: str | None = None, can_stop: bool | None = None,
                 ts: float | None = None) -> dict:
    return make("task.updated", session,
                {"task_id": task_id, "kind": kind, "status": status,
                 "parent_turn_id": parent_turn_id, "title": title,
                 "can_stop": can_stop}, ts=ts)


def checkpoint_created(session: str, checkpoint_id: str, *,
                       files: int | None = None, note: str | None = None,
                       turn_id: str | None = None,
                       ts: float | None = None) -> dict:
    return make("checkpoint.created", session,
                {"checkpoint_id": checkpoint_id, "files": files,
                 "note": note}, turn_id=turn_id, ts=ts)


def checkpoint_restored(session: str, checkpoint_id: str, *,
                        files: int | None = None, by: str | None = None,
                        command_id: str | None = None,
                        ts: float | None = None) -> dict:
    return make("checkpoint.restored", session,
                {"checkpoint_id": checkpoint_id, "files": files, "by": by},
                command_id=command_id, ts=ts)


def changes_updated(session: str, files: list, *,
                    turn_id: str | None = None,
                    ts: float | None = None) -> dict:
    """``files`` is ``[{path, kind, added?, removed?}]``."""
    return make("changes.updated", session, {"files": files},
                turn_id=turn_id, ts=ts)


def queue_updated(session: str, items: list, *,
                  ts: float | None = None) -> dict:
    """``items`` is ``[{command_id, text, by?, mode?, attachments?}]``,
    oldest first."""
    return make("queue.updated", session, {"items": items}, ts=ts)


def command_receipt(session: str, command_id: str, status: str, *,
                    reason: str | None = None, op: str | None = None,
                    ts: float | None = None) -> dict:
    return make("command.receipt", session,
                {"status": status, "reason": reason, "op": op},
                command_id=command_id, ts=ts)


def command_result(session: str, command_id: str, op: str, ok: bool, *,
                   note: str | None = None, degraded: bool | None = None,
                   cancelled: bool | None = None,
                   request_id: str | None = None,
                   launch_id: str | None = None,
                   ts: float | None = None) -> dict:
    return make("command.result", session,
                {"op": op, "ok": ok, "note": note, "degraded": degraded,
                 "cancelled": cancelled, "request_id": request_id,
                 "launch_id": launch_id},
                command_id=command_id, ts=ts)


def usage_updated(session: str, *, turn_id: str | None = None,
                  ts: float | None = None, **counts: Any) -> dict:
    """``counts``: ``input_tokens``, ``output_tokens``,
    ``cache_read_tokens``, ``cache_write_tokens``, ``context_tokens``,
    ``context_window``, ``cost_usd``."""
    return make("usage.updated", session, dict(counts),
                turn_id=turn_id, ts=ts)


def context_compacted(session: str, *, summary: str | None = None,
                      tokens_before: int | None = None,
                      turn_id: str | None = None,
                      ts: float | None = None) -> dict:
    return make("context.compacted", session,
                {"summary": summary, "tokens_before": tokens_before},
                turn_id=turn_id, ts=ts)


def error(session: str, code: str, message: str, *,
          fatal: bool | None = None, turn_id: str | None = None,
          command_id: str | None = None, ts: float | None = None) -> dict:
    return make("error", session,
                {"code": code, "message": message, "fatal": fatal},
                turn_id=turn_id, command_id=command_id, ts=ts)


def notify(session: str, kind: str, message: str, *,
           status: str | None = None, ts: float | None = None) -> dict:
    return make("notify", session,
                {"kind": kind, "message": message, "status": status}, ts=ts)
