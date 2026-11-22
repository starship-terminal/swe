"""Wire schema for ``swe remote``: one protocol for phone↔hub and satellite↔hub.

Everything that crosses a socket is a plain dict built here so the shape
has one home. Two flows share it: the phone talks REST + SSE to the hub
(``swe remote serve``) and satellites forward the same event envelopes
upstream from ``swe remote agent``.

Envelope::

    {"v": 1, "type": "message.appended", "session": "claude:abc",
     "machine": "macbook", "seq": 41, "ts": 1760000000.0,
     "data": {...}}

``seq`` is assigned per session key by :mod:`journal` at write time; it is
what ``?last=seq`` replays from.
"""

from __future__ import annotations

import hashlib
import re
import time

PROTOCOL_VERSION = 1

# -- sync v2 (docs/specs/G9-sync-v2.md) -----------------------------
# The second protocol a client can ask a session stream for: v2 events
# (``adapters/events.py``), catch-up by ``last`` and ``epoch``, a
# snapshot, live text recovered apart from the journal, and pings.
SYNC_VERSION = 2
# What ``GET /v1/status`` lists as ``protocols``.
PROTOCOLS = (PROTOCOL_VERSION, SYNC_VERSION)
# A client asks for v2 with ``?v=2`` or this header.
VERSION_HEADER = "X-SWE-Protocol"
# What ``GET /v1/status`` lists as ``features``, for a client that
# checks before it relies on one. ``session_runtime`` joins the list
# when the hub runs sessions through keepers; ``STORE_FEATURES`` are
# listed only then too. ``require_keeper``: a message post with
# ``require_keeper: true`` is refused with 409 ``not_kept`` instead of
# taking the legacy route. ``command_lookup``:
# ``GET /v1/sessions/{key}/commands/{id}`` reads a stored command.
FEATURE_REQUIRE_KEEPER = "require_keeper"
FEATURE_COMMAND_LOOKUP = "command_lookup"
# ``session_options`` (G32): kept sessions journal ``session.options``,
# a snapshot carries ``options``, and ``POST .../command`` takes
# ``set_option``.
FEATURE_SESSION_OPTIONS = "session_options"
FEATURES = ("sync_v2", "snapshot", "live_resume", "delta_offset", "ping",
            "command_id", FEATURE_REQUIRE_KEEPER, FEATURE_COMMAND_LOOKUP,
            FEATURE_SESSION_OPTIONS)
# Listed only while the session runtime's store is open, like
# ``session_runtime``.
STORE_FEATURES = (FEATURE_REQUIRE_KEEPER, FEATURE_COMMAND_LOOKUP,
                  FEATURE_SESSION_OPTIONS)
FEATURE_RUNTIME = "session_runtime"
# A v2 stream sends ``ping {ts, head}`` this often; a client treats
# ``SILENCE_S`` with no frame at all as a dead connection.
PING_S = 10.0
SILENCE_S = 25.0
# A catch-up replays at most this many seqs; a wider gap gets a
# snapshot instead.
REPLAY_MAX = 500
# How many timeline events a snapshot carries.
SNAPSHOT_MESSAGES = 200
# Frames a v2 stream sends that are not events of ``adapters/events.py``.
SYNC_FRAME_TYPES = frozenset({"session.snapshot", "session.row", "ping"})


def wants_v2(args, headers) -> bool:
    """Whether a request asks for sync v2: ``?v=2``, or the
    ``X-SWE-Protocol: 2`` header. ``args`` is ``parse_qs``'s dict;
    ``headers`` anything with ``get``. Anything else, an unreadable
    value included, is a client of today's protocol."""
    values = (args or {}).get("v") or []
    asked = values[0] if values else None
    if asked is None and headers is not None:
        asked = headers.get(VERSION_HEADER)
    return isinstance(asked, str) and asked.strip() == str(SYNC_VERSION)


def sync_frame(frame_type: str, session: str, data: dict, *,
               ts: float | None = None) -> dict:
    """One of the v2 stream's own frames (``SYNC_FRAME_TYPES``). It has
    no ``seq``: nothing in it is journaled, so it never moves a
    client's cursor."""
    return {
        "v": SYNC_VERSION,
        "type": frame_type,
        "session": session,
        "ts": ts if ts is not None else time.time(),
        "data": data,
    }


# ``client_id`` on a message POST lets the caller match the async
# ``command.result`` to an optimistic bubble it already drew.
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# A compaction is not an event type of its own: it arrives as
# ``message.appended`` with ``kind: "compaction"`` (see MESSAGE_KINDS).
EVENT_TYPES = frozenset(
    {
        "message.appended",
        "message.delta",
        "session.status",
        "approval.requested",
        "approval.resolved",
        "machine.online",
        "machine.offline",
        "notify",
    }
)

# A harness name in a launch body: a registry key, nothing else.
_HARNESS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# A launch's ``model``/``effort``: one short token. The value is typed
# into a pane as ``/model <v>``, so whitespace — which would smuggle a
# second command or land the rest as prompt text — is never allowed.
_LAUNCH_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}")


def _launch_value(body: dict, field: str) -> str | None:
    """An optional launch field (``model``, ``effort``): None when
    absent or empty, else a single-token string."""
    value = body.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _LAUNCH_VALUE_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a short name "
            "(letters, digits and . _ : / + @ -)"
        )
    return value


def _launch_provider(body: dict) -> str | None:
    """An optional ``provider`` slug: None when absent or empty. Only
    the slug travels; the launching machine reads its own key."""
    from swe.remote.launch_provider import SLUG_RE

    value = body.get("provider")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise ValueError("provider must be a provider slug "
                         "(lowercase letters, digits and . _ -)")
    return value


def _launch_bool(body: dict, field: str) -> bool:
    """An optional launch switch (``yolo``): False when absent, else a
    strict bool — a stringly value is refused rather than coerced."""
    value = body.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false")
    return value

# Cap on a first prompt; the phone's composer never gets near it.
# A wait for a session to go idle (a queue-mode send on the hub or a
# satellite, a when_idle detach) polls a fresh status this often and
# gives up after the cap, failing honestly.
IDLE_POLL_S = 2.0
IDLE_WAIT_MAX_S = 1800

MAX_LAUNCH_PROMPT = 32_000

# RemoteMessage.kind values. ``tool`` payloads pair over ``call_id``;
# ``result_ref`` on a tool_result names the call it answers.
MESSAGE_KINDS = frozenset(
    {
        "user",
        "assistant",
        "thinking",
        "tool_use",
        "tool_result",
        "compaction",
        "system",
        "approval",
        "error",
    }
)

# POST /v1/sessions/{id}/message ``mode``: queue waits for the turn to end;
# steer injects mid-turn where the channel allows, else degrades to queue.
MESSAGE_MODES = frozenset({"queue", "steer"})

# POST /v1/sessions/{id}/control ``op`` values.
CONTROL_OPS = frozenset(
    {"interrupt", "compact", "clear", "model", "effort", "attach",
     "detach", "kill"}
)

# ``detach``'s ``mode``: ``now`` closes the tmux session (the default, so
# older clients keep their behaviour), ``when_idle`` waits for the turn
# to end first, ``keep`` stops supervising but leaves tmux running.
DETACH_MODES = frozenset({"now", "when_idle", "keep"})

NOTIFY_KINDS = frozenset({"needs_you", "finished", "error", "limit"})

# Session ownership: ``attached`` runs under our supervisor (read+write),
# ``observed`` belongs to a human terminal (read + notify only).
OWNERSHIP_ATTACHED = "attached"
OWNERSHIP_OBSERVED = "observed"

# Hub-scoped session keys for satellite rows: ``tool:sid@machine``.
REMOTE_SEP = "@"


def remote_key(machine_id: str, key: str) -> str:
    """Namespace a satellite-local session key for hub-wide use."""
    return f"{key}{REMOTE_SEP}{machine_id}"


def split_remote_key(key: str) -> tuple[str, str | None]:
    """``(local_key, machine_id)`` — machine_id is None for local keys."""
    base, sep, mid = key.rpartition(REMOTE_SEP)
    return (base, mid) if sep and mid else (key, None)


def session_key(tool_name: str, session_id: str, path: str = "") -> str:
    """``tool:sid`` — the hub-scoped name one session is streamed under.

    A session with no recorded id (Pi keys on path, a blank claude row)
    keys on a path digest so it still lands in one journal.
    """
    sid = (session_id or "").strip()
    if not sid:
        digest = hashlib.sha1((path or "").encode("utf-8")).hexdigest()[:12]
        sid = f"path-{digest}"
    return f"{tool_name}:{sid}"


def url_host(host: str) -> str:
    """``host`` as a URL's authority spells it: an IPv6 literal goes in
    brackets (once), or ``::1:8477`` can't be told from its port."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def key_slug(key: str) -> str:
    """Filesystem/URL-safe name for a session key (journal files, urls)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def event(
    event_type: str,
    session: str,
    machine: str,
    data: dict,
    *,
    ts: float | None = None,
) -> dict:
    """Build one wire event. ``seq`` is stamped by the journal on append."""
    return {
        "v": PROTOCOL_VERSION,
        "type": event_type,
        "session": session,
        "machine": machine,
        "seq": 0,
        "ts": ts if ts is not None else time.time(),
        "data": data,
    }


def message(
    kind: str,
    text: str,
    *,
    ts: float | None = None,
    tool: dict | None = None,
    result_ref: str | None = None,
) -> dict:
    """The ``data`` payload of a ``message.appended`` event."""
    return {
        "kind": kind,
        "text": text,
        "ts": ts if ts is not None else time.time(),
        "tool": tool,
        "result_ref": result_ref,
    }


def delta_event(
    session: str,
    machine: str,
    msg_id: str,
    kind: str,
    text: str,
    seq: int,
    *,
    done: bool = False,
    ts: float | None = None,
) -> dict:
    """A ``message.delta`` envelope: live text for one streamed message.

    It carries no envelope ``seq``: deltas are never journaled, so they
    must not move a client's replay cursor (the SSE frame has no
    ``id:`` either). ``data.seq`` counts this message's deltas from 1.
    """
    ev = event("message.delta", session, machine, {
        "id": msg_id, "kind": kind, "text": text, "seq": seq,
        "done": done,
    }, ts=ts)
    del ev["seq"]
    return ev


def command(
    op: str,
    session: str,
    machine: str,
    command_id: str,
    data: dict,
    *,
    ts: float | None = None,
) -> dict:
    """A downstream command envelope, journaled under ``_sat_<mid>``."""
    return {
        "v": PROTOCOL_VERSION,
        "type": "command",
        "session": session,
        "machine": machine,
        "seq": 0,
        "ts": ts if ts is not None else time.time(),
        "data": {"op": op, "command_id": command_id, **data},
    }


def error(code: str, message_text: str) -> dict:
    """The REST error body: ``{"error": {"code", "message"}}``."""
    return {"error": {"code": code, "message": message_text}}


def _message_text(text, field: str) -> str:
    """A message body's text: a string with something besides spaces."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return text


def parse_message_command(body: dict) -> dict:
    """Validate a ``POST .../message`` body; raises ValueError on bad input."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    text = _message_text(body.get("text"), "message.text")
    mode = body.get("mode")
    if mode is None or mode == "":
        mode = "queue"
    # Type-check before every set lookup below: a JSON list or object is
    # unhashable, and the TypeError would drop the request unanswered.
    if not isinstance(mode, str) or mode not in MESSAGE_MODES:
        raise ValueError(f"message.mode must be one of {sorted(MESSAGE_MODES)}")
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list) or not all(
        isinstance(a, str) for a in attachments
    ):
        raise ValueError("message.attachments must be a list of staged refs")
    client_id = body.get("client_id")
    if client_id is None:
        client_id = ""
    elif (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 64
        or not _CLIENT_ID_RE.fullmatch(client_id)
    ):
        raise ValueError(
            "message.client_id must be 1-64 chars of [A-Za-z0-9_-]"
        )
    return {
        "text": text, "mode": mode, "attachments": attachments,
        "client_id": client_id,
    }


# What ``POST .../queue`` can do to one waiting queue-mode send.
QUEUE_OPS = frozenset({"steer", "cancel", "edit"})


def parse_queue_action(body: dict) -> dict:
    """Validate a ``POST .../queue`` body: ``{client_id, op, text?}``.

    ``text`` is required for ``edit`` (validated like a message's) and
    ignored otherwise.
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    client_id = body.get("client_id")
    if (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 64
        or not _CLIENT_ID_RE.fullmatch(client_id)
    ):
        raise ValueError("queue.client_id must be 1-64 chars of [A-Za-z0-9_-]")
    op = body.get("op")
    # A list or object op is unhashable: check the type before ``in``.
    if not isinstance(op, str) or op not in QUEUE_OPS:
        raise ValueError(f"queue.op must be one of {sorted(QUEUE_OPS)}")
    text = None
    if op == "edit":
        text = _message_text(body.get("text"), "queue.text")
    return {"client_id": client_id, "op": op, "text": text}


def parse_approval_command(body: dict) -> dict:
    """Validate a ``POST .../approval`` body."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    request_id = body.get("request_id")
    decision = body.get("decision")
    if not request_id or not isinstance(request_id, str):
        raise ValueError("approval.request_id is required")
    if not isinstance(decision, str) or not (
        decision in {"allow", "deny", "always"}
        or (decision.isdigit() and len(decision) <= 2)
    ):
        raise ValueError(
            "approval.decision must be allow|deny|always or a menu row number"
        )
    return {"request_id": request_id, "decision": decision}


def parse_control_command(body: dict) -> dict:
    """Validate a ``POST .../control`` body."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    op = body.get("op")
    if not isinstance(op, str) or op not in CONTROL_OPS:
        raise ValueError(f"control.op must be one of {sorted(CONTROL_OPS)}")
    value = body.get("value")
    if value is not None and not isinstance(value, str):
        raise ValueError("control.value must be a string when given")
    if op in {"model", "effort"}:
        # Typed into the pane as ``/model <value>``: held to the launch
        # rule, so a newline can't send a second line to the TUI.
        value = _launch_value(body, "value")
    cmd = {"op": op, "value": value}
    if op == "detach":
        mode = body.get("mode")
        if mode is None:
            mode = "now"
        if not isinstance(mode, str) or mode not in DETACH_MODES:
            raise ValueError(
                f"detach mode must be one of {sorted(DETACH_MODES)}"
            )
        cmd["mode"] = mode
    return cmd


def parse_launch_command(body: dict) -> dict:
    """Validate a ``POST /v1/sessions`` body; raises ValueError on bad input.

    Checks only what holds on every machine: a harness name, an absolute
    folder, a prompt. Which harnesses can start is the launching
    machine's call (``catalog.launchable_harnesses``). Whether the folder exists is the launching
    machine's call (``supervise.launch_new``), since a satellite's disk
    is not the hub's.
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    harness = body.get("harness") or body.get("tool") or ""
    if not isinstance(harness, str) or not _HARNESS_RE.fullmatch(harness):
        raise ValueError("harness must be a harness name")
    cwd = body.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        raise ValueError("cwd must be an absolute path")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_LAUNCH_PROMPT:
        raise ValueError(f"prompt is over {MAX_LAUNCH_PROMPT} characters")
    machine = body.get("machine") or ""
    if not isinstance(machine, str):
        raise ValueError("machine must be a string")
    return {"machine": machine, "harness": harness, "cwd": cwd,
            "prompt": prompt,
            "model": _launch_value(body, "model"),
            "effort": _launch_value(body, "effort"),
            "yolo": _launch_bool(body, "yolo"),
            "provider": _launch_provider(body)}
