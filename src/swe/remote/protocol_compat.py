"""v2 session events as today's v1 frames, for clients that predate v2.

:func:`to_v1` maps one canonical v2 event
(:mod:`swe.remote.adapters.events`) to the v1 events
(:mod:`swe.remote.protocol`) a current client already draws, built with
``protocol``'s own helpers so the shapes match ``tail.py`` and the
injectors field for field:

==================== ===================================================
v2                   v1
==================== ===================================================
message.completed    message.appended, kind user, assistant or system;
                     ``attachments`` folded into the text as v1 sends
                     them (``message_text``)
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

A v2 client reads the other way through :func:`to_v2`: a session no
keeper holds (a terminal session, and the earlier history of one
continued in SWE) has a journal of v1 events, and a ``v=2`` stream
serves each as the v2 event it stands for
(docs/specs/G9-sync-v2.md has the table).

Pure: no I/O, no clock, and the input is never changed.
"""

from __future__ import annotations

import json

from swe.remote import protocol
from swe.remote.adapters import events as _ev
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
    "session.caps", "session.options", "turn.started", "turn.ended",
    "tool.updated",
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
    "" when there are none. A valid v2 event is always strict JSON
    (``events.validate``), so this cannot fail."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


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


def message_text(text: str, attachments, *, at_refs: bool = False) -> str:
    """A v2 message's text with its ``attachments`` folded in the way v1
    carries them: exactly ``attachments.prompt_text``'s form, the text
    the injectors send and the transcript then records. ``at_refs`` is
    the session harness's ``attachments.at_refs``: ``@/abs/path`` refs
    when true, ``[Attached file: /abs/path]`` otherwise."""
    paths = [str(p) for p in attachments or []]
    if not paths:
        return text
    if at_refs:
        refs = " ".join(f"@{path}" for path in paths)
    else:
        refs = " ".join(f"[Attached file: {path}]" for path in paths)
    return f"{refs} {text}" if text else refs


def to_v1(event: dict, *, machine: str = "",
          at_refs: bool = False) -> list[dict]:
    """The v1 events a pre-v2 client should get for ``event``: an empty
    list for a type v1 has no frame for, else one event. ``machine`` is
    the v1 envelope's machine id; ``at_refs`` is the session harness's
    ``attachments.at_refs`` (the caller resolves it, so this stays
    pure). Raises ValueError for an event that is not v2."""
    if not isinstance(event, dict) or event.get("v") != V2:
        raise ValueError("not a v2 event")
    etype = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if etype in DROPPED:
        return []
    if etype == "message.completed":
        text = message_text(data["text"], data.get("attachments"),
                            at_refs=at_refs)
        return [_appended(event, machine, data["kind"], text)]
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


def stream_v1(events, *, machine: str = "",
              at_refs: bool = False) -> list[dict]:
    """:func:`to_v1` over a run of events, in order."""
    out: list[dict] = []
    for event in events:
        out.extend(to_v1(event, machine=machine, at_refs=at_refs))
    return out


# ---------------------------------------------------------------------
# v1 to v2, for a v2 client reading a journal of v1 events.

# session.status -> session.state: the inverse of STATUS_FOR_STATE,
# with ``done`` read as idle (parked and closed are a keeper's states).
STATE_FOR_STATUS = {
    "active": "running",
    "done": "idle",
    "followup": "waiting",
    "error": "agent_down",
}

# v1's names for the two commands v2 renamed (``runtime._V1_OPS``).
_V2_OPS = {"message": "send", "approval": "respond"}

# message.appended kinds that are a v2 ``message.completed``. v1's
# ``approval`` kind is a line of transcript, not an open request.
_COMPLETED_KINDS = {"user": "user", "assistant": "assistant",
                    "system": "system", "approval": "system"}


def _v2(event: dict, etype: str, data: dict, *,
        command_id: str | None = None) -> dict | None:
    """The v2 envelope for a v1 ``event``, or None when what comes out
    is not a valid v2 event (a journal can hold any older shape)."""
    seq = event.get("seq")
    ts = event.get("ts")
    out: dict = {
        "v": V2, "type": etype,
        "session": str(event.get("session") or ""),
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool)
        and seq > 0 else 0,
        "ts": ts if isinstance(ts, (int, float))
        and not isinstance(ts, bool) and ts >= 0 else 0,
    }
    if etype in _ev.TRANSIENT_TYPES:
        del out["seq"]
    if command_id:
        out["command_id"] = command_id
    out["data"] = {k: v for k, v in data.items() if v is not None}
    return None if _ev.validate(out) else out


def _number(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) and value >= 0 else None


def to_v2(event: dict, *, msg_id: str | None = None,
          offset: int | None = None) -> dict | None:
    """The v2 event a v2 client should get for one v1 ``event``, or
    None where v2 has no event for it. A v2 event comes back as it is.

    v1 messages carry no id. ``msg_id`` names the message when the
    caller knows it (the live text this message completes); otherwise
    it is ``j<seq>``, which is stable for a journaled event. ``offset``
    is where a delta's text starts in its message (v1 deltas do not
    say; the hub counts). The result keeps the event's ``seq``; an
    event with none gets ``seq`` 0, which a client never takes as a
    cursor.
    """
    if not isinstance(event, dict):
        return None
    if event.get("v") == V2:
        return event
    etype = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    ident = msg_id or f"j{event.get('seq') or 0}"
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    if etype == "message.appended":
        kind = data.get("kind")
        if kind in _COMPLETED_KINDS:
            return _v2(event, "message.completed", {
                "id": ident, "kind": _COMPLETED_KINDS[kind], "text": text})
        if kind == "thinking":
            return _v2(event, "thinking.completed", {
                "id": ident, "text": text,
                "duration_s": _number(data.get("duration_s"))})
        if kind == "tool_use":
            tool = data.get("tool") if isinstance(data.get("tool"), dict) \
                else {}
            return _v2(event, "tool.started", {
                "call_id": str(tool.get("call_id") or "") or ident,
                "name": str(tool.get("name") or "") or "tool",
                "summary": tool.get("args_summary")
                if isinstance(tool.get("args_summary"), str) else None,
                "input": tool.get("args_full") or None})
        if kind == "tool_result":
            return _v2(event, "tool.completed", {
                "call_id": str(data.get("result_ref") or "") or ident,
                "status": "ok", "output": text})
        if kind == "compaction":
            return _v2(event, "context.compacted", {"summary": text})
        if kind == "error":
            return _v2(event, "error", {"code": "error", "message": text})
        return None
    if etype == "message.delta":
        kind = data.get("kind")
        return _v2(event, "message.delta", {
            "id": str(data.get("id") or "") or ident,
            "kind": kind if kind in _ev.DELTA_KINDS else "assistant",
            "text": text, "seq": data.get("seq"), "offset": offset,
            "done": data.get("done") is True})
    if etype == "session.status":
        state = STATE_FOR_STATUS.get(data.get("status"))
        if state is None:
            return None
        return _v2(event, "session.state", {"state": state})
    if etype == "approval.requested":
        options = []
        for n, row in enumerate(data.get("options") or [], 1):
            if not isinstance(row, dict):
                continue
            options.append({k: v for k, v in (
                ("id", str(row.get("n") or n)),
                ("label", str(row.get("label") or "")),
                ("kind", row.get("kind") if isinstance(row.get("kind"), str)
                 and row.get("kind") else None),
                ("standing", True if row.get("standing") is True else None),
            ) if v is not None})
        return _v2(event, "approval.requested", {
            "request_id": str(data.get("request_id") or ""),
            "kind": "tool", "prompt": str(data.get("prompt") or ""),
            "options": options or None,
            "auto": True if data.get("auto") is True else None})
    if etype == "approval.resolved":
        source = data.get("source")
        if not source:
            source = "user"  # v1: a device's answer carries no source
        elif source not in _ev.APPROVAL_SOURCES:
            source = "harness"  # gone, detached, dismissed, keeper_lost
        decision = data.get("decision")
        return _v2(event, "approval.resolved", {
            "request_id": str(data.get("request_id") or ""),
            "source": source,
            "decision": decision if decision in _ev.DECISIONS else None})
    if etype == "command.result":
        command_id = str(data.get("command_id") or data.get("client_id")
                         or "")
        op = str(data.get("op") or "")
        return _v2(event, "command.result", {
            "op": _V2_OPS.get(op, op), "ok": data.get("ok") is True,
            "note": data.get("note") if isinstance(data.get("note"), str)
            and data.get("note") else None,
            "degraded": True if data.get("degraded") is True else None,
            "cancelled": True if data.get("cancelled") is True else None,
            "request_id": data.get("request_id")
            if isinstance(data.get("request_id"), str)
            and data.get("request_id") else None,
            "launch_id": data.get("launch_id")
            if isinstance(data.get("launch_id"), str)
            and data.get("launch_id") else None,
        }, command_id=command_id)
    if etype == "notify":
        return _v2(event, "notify", {
            "kind": data.get("kind"),
            "message": str(data.get("message") or ""),
            "status": data.get("status")
            if isinstance(data.get("status"), str)
            and data.get("status") else None})
    return None
