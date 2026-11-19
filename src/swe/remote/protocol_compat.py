"""v2 session events as today's v1 frames, for clients that predate v2.

:func:`to_v1` maps one canonical v2 event
(:mod:`swe.remote.adapters.events`) to the v1 events
(:mod:`swe.remote.protocol`) a current client already draws, built with
``protocol``'s own helpers so the shapes match ``tail.py`` and the
injectors field for field:

==================== ===================================================
v2                   v1
==================== ===================================================
message.completed    message.appended, kind user, assistant or system
thinking.completed   message.appended, kind thinking (+ duration_s)
tool.started         message.appended, kind tool_use (tool card)
tool.completed       message.appended, kind tool_result (result_ref)
context.compacted    message.appended, kind compaction
error                message.appended, kind error
message.delta        message.delta (no envelope seq, as v1)
session.state        session.status: running active, idle done,
                     waiting followup, agent_down error, parked and
                     closed done; starting dropped
approval.requested   approval.requested {request_id, prompt, options
                     [{n, label, kind?, standing?}], auto?}
approval.resolved    approval.resolved {request_id, decision?, source?}
command.result       command.result {command_id, op, ok, note, degraded,
                     client_id, ...}
notify               notify
everything else      dropped: session.caps, turn.*, tool.updated,
                     input.requested, plan.updated, task.updated,
                     checkpoint.*, changes.updated, queue.updated,
                     command.receipt, usage.updated
==================== ===================================================

Each v2 event becomes at most one v1 event, which keeps the v2 ``seq``:
v1 clients dedupe ``message.appended`` by seq, so two frames sharing one
would lose the second. Dropped events leave gaps in the seq numbers a
v1 client sees; every client only keeps the highest seq as its cursor,
so a gap is harmless (docs/specs/G3-adapter-protocol.md).

Pure: no I/O, no clock, and the input is never changed.
"""

from __future__ import annotations

import json

from swe.remote import protocol
from swe.remote.adapters.events import VERSION as V2

# session.state -> session.status (sessions/status.py's values).
STATUS_FOR_STATE = {
    "running": "active",
    "idle": "done",
    "waiting": "followup",
    "agent_down": "error",
    "parked": "done",
    "closed": "done",
}

DROPPED = frozenset({
    "session.caps", "turn.started", "turn.ended", "tool.updated",
    "input.requested", "plan.updated", "task.updated",
    "checkpoint.created", "checkpoint.restored", "changes.updated",
    "queue.updated", "command.receipt", "usage.updated",
})


def _v1(event: dict, etype: str, data: dict, machine: str) -> dict:
    out = protocol.event(etype, str(event.get("session") or ""), machine,
                         data, ts=event.get("ts"))
    out["seq"] = event.get("seq", 0)
    return out


def _appended(event: dict, machine: str, kind: str, text: str,
              **extra) -> dict:
    data = protocol.message(kind, text, ts=event.get("ts"),
                            tool=extra.pop("tool", None),
                            result_ref=extra.pop("result_ref", None))
    data.update(extra)
    return _v1(event, "message.appended", data, machine)


def _args_full(value) -> str:
    """``tail._summarize_args``'s ``args_full``: the arguments as JSON,
    "" when there are none."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _option_rows(options) -> list[dict]:
    """v2 options as v1 menu rows, numbered by position like
    ``acp._menu_row``; a v1 answer naming row ``n`` means the n-th
    option."""
    rows = []
    for n, opt in enumerate(options or [], 1):
        row = {"n": str(n), "label": str(opt.get("label") or opt["id"])}
        if opt.get("kind"):
            row["kind"] = opt["kind"]
        if opt.get("standing"):
            row["standing"] = True
        rows.append(row)
    return rows


def to_v1(event: dict, *, machine: str = "") -> list[dict]:
    """The v1 events a pre-v2 client should get for ``event``: an empty
    list for a type v1 has no frame for, else one event. ``machine`` is
    the v1 envelope's machine id. Raises ValueError for an event that is
    not v2."""
    if not isinstance(event, dict) or event.get("v") != V2:
        raise ValueError("not a v2 event")
    etype = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if etype in DROPPED:
        return []
    if etype == "message.completed":
        return [_appended(event, machine, data["kind"], data["text"])]
    if etype == "thinking.completed":
        extra = {}
        if data.get("duration_s") is not None:
            extra["duration_s"] = data["duration_s"]
        return [_appended(event, machine, "thinking", data["text"], **extra)]
    if etype == "tool.started":
        name = data["name"]
        return [_appended(event, machine, "tool_use", "", tool={
            "name": name,
            "call_id": data["call_id"],
            "args_summary": data.get("summary") or name,
            "args_full": _args_full(data.get("input")),
        })]
    if etype == "tool.completed":
        return [_appended(event, machine, "tool_result",
                          data.get("output") or "",
                          result_ref=data["call_id"] or None)]
    if etype == "context.compacted":
        return [_appended(event, machine, "compaction",
                          data.get("summary") or "context compacted")]
    if etype == "error":
        return [_appended(event, machine, "error", data["message"])]
    if etype == "message.delta":
        return [protocol.delta_event(
            str(event.get("session") or ""), machine, data["id"],
            data["kind"], data["text"], data["seq"],
            done=bool(data.get("done")), ts=event.get("ts"))]
    if etype == "session.state":
        status = STATUS_FOR_STATE.get(data["state"])
        if status is None:
            return []
        return [_v1(event, "session.status", {"status": status}, machine)]
    if etype == "approval.requested":
        out = {"request_id": data["request_id"],
               "prompt": data["prompt"],
               "options": _option_rows(data.get("options"))}
        if data.get("auto"):
            out["auto"] = True
        return [_v1(event, "approval.requested", out, machine)]
    if etype == "approval.resolved":
        out = {"request_id": data["request_id"]}
        if data.get("decision"):
            out["decision"] = data["decision"]
        # A device's answer carried no source in v1 (hub.py's resolve).
        if data.get("source") and data["source"] != "user":
            out["source"] = data["source"]
        return [_v1(event, "approval.resolved", out, machine)]
    if etype == "command.result":
        command_id = str(event.get("command_id") or "")
        out = {"command_id": command_id, "op": data["op"],
               "ok": data["ok"], "note": data.get("note") or "",
               "degraded": bool(data.get("degraded")),
               # v2's command_id is v1's client_id: the id the client
               # sent and matches its optimistic bubble on.
               "client_id": command_id}
        for name in ("request_id", "launch_id"):
            if data.get(name):
                out[name] = data[name]
        if data.get("cancelled"):
            out["cancelled"] = True
        return [_v1(event, "command.result", out, machine)]
    if etype == "notify":
        out = {"kind": data["kind"], "message": data["message"]}
        if data.get("status"):
            out["status"] = data["status"]
        return [_v1(event, "notify", out, machine)]
    raise ValueError(f"unknown v2 event type {etype!r}")


def stream_v1(events, *, machine: str = "") -> list[dict]:
    """:func:`to_v1` over a run of events, in order."""
    out: list[dict] = []
    for event in events:
        out.extend(to_v1(event, machine=machine))
    return out
