"""Read path: transcript bytes → ``message.appended`` events.

Copy of the ``search_index`` files-row idea: a byte-offset cursor per
transcript, stat → read appended bytes → decode new records → emit
``RemoteMessage`` dicts. The upgrade over ``reports/transcripts.py`` is
detail: that layer strips thinking and collapses tool calls for reports;
a chat UI wants the raw block detail (Claude ``thinking`` /
``redacted_thinking`` blocks and compact boundaries, Codex reasoning
items and ``compacted`` payloads, opencode part types).

Three source kinds cover every parser the registry knows:

- ``JsonlTailSource`` — incremental byte-offset reads over a transcript
  file (claude, codex, pi, cursor, droid, grok, freebuff).
  ``MirrorCopySource`` reads swe's mirror copy the same way when the
  harness deleted the transcript, and never tails it.
- ``OpencodeSource`` — SQLite ``part`` rows with a ``time_created``
  cursor, plus opencode 2.x ``session_message`` rows with a ``seq``
  cursor (opencode, kilo share the drizzle schema).
- ``SnapshotSource`` — ``read_transcript`` re-reads diffed by message
  fingerprint, for the remaining JSON/SQLite stores (continue, cline,
  gemini, copilot, devin, forge, crush).

Polling is "~1s while ``sessions/status.py`` says active, back off idle",
plus a short hot window after the hub or a satellite writes to a session
(``TailManager.wake``), and ``_redact_secrets`` runs on every text before it can leave the
process — transcript text is not user-originated.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import dataclasses
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from swe.paths import expand_user, remote_dir_for
from swe.remote import injected, protocol, supervise
from swe.reports import transcripts as _tr
from swe.sessions import opencode_store
from swe.sessions.models import Session

# Poll cadence: tight while the session is mid-turn, relaxed otherwise.
POLL_ACTIVE_S = 1.0
POLL_IDLE_S = 8.0
# After a write to a session (a message going out, an approval answer, a
# control op, an attach) its tailer polls at once and then this fast for
# HOT_WINDOW_S, so the echo and first reply show up without waiting out
# an idle interval the catalog has not yet flipped to active. Each new
# write re-arms the window. Snapshot sources cap this at POLL_ACTIVE_S
# (see SnapshotSource.hot_poll_s).
POLL_HOT_S = 0.25
HOT_WINDOW_S = 15.0
# Low power: no client has asked for anything lately, no SSE stream is
# open and nothing pushes to ntfy (``TailManager.set_low_power``). The
# transcripts are durable on disk, so a slower poll only delays the
# journal's copy; a client's next request ends low power at once and
# every tailer polls straight away.
POLL_ACTIVE_LOW_S = 5.0
POLL_IDLE_LOW_S = 60.0
# A hot session, or one whose transcript just grew, has its status
# re-probed (one session through sessions/status.py and its tail cache)
# at most this often, instead of waiting for the 10 s catalog refresh.
STATUS_PROBE_S = 1.0
# Tmux-pane approval prompt watch — a subprocess per attached session,
# so slower than the transcript poll.
PANE_APPROVAL_S = 3.0


def _approval_prompt_line(pane: str) -> str:
    """A one-line summary of the on-screen permission question.

    Pane text is user-originated like tool args are: it can carry a
    ``Bearer sk-...`` the agent asked about, so it is redacted before it
    can reach the journal, a phone, or an ntfy body. Menu rows are never
    the question: when the banner scrolled off, "2 Yes, allow `sudo`
    commands" must not stand in for it.
    """
    ctx = supervise.running_context(pane)
    if ctx:
        return _tr._redact_secrets(ctx)[:140]
    _, menu_at = supervise.menu_block(pane)
    _, choice_at, _ = supervise.choice_block(pane)
    # The answers come from the lowest block, so the question must too:
    # zoning off a stale menu that scrolled up would pair the old
    # question with the live options beneath it.
    menu_at = max(menu_at, choice_at)
    lines = pane.splitlines()
    # The question nearest the menu is its question — earlier lines can
    # hold questions of their own ("Which branch?"). Without a menu the
    # first question-looking line stands, as before.
    zone = lines[:menu_at] if menu_at >= 0 else lines
    pick = None
    for line in zone:
        text = line.strip()
        if not text or supervise._MENU_OPTION.match(line):
            continue
        low = text.lower()
        if ("do you want" in low or "allow" in low
                or "permission" in low or text.endswith("?")):
            pick = text
            if menu_at < 0:
                break
    if pick is not None:
        return _tr._redact_secrets(pick)[:140]
    return "Approval requested"

# How many of the most recent sessions get a live tailer.
TAIL_MAX_SESSIONS = 100

# A session's journal and cursor files, untouched this long and not
# being tailed, are deleted by ``TailManager.sweep_stale``. Without it
# every session ever tailed kept up to 16 MB of journal forever.
STALE_FILE_AGE_S = 30 * 86_400

# Partial-line safety: never decode more than this many bytes of tail at
# once; the remainder waits for the next poll.
_MAX_CHUNK = 4 << 20

# ``JsonlTailSource.continue_from``: how much of the old transcript
# before its cursor identifies the boundary, and how far from the same
# offset the new file is searched for it.
_BOUNDARY_BYTES = 4096
_HANDOFF_SPAN = 1 << 20

# How much of a transcript's head the tailer keeps to tell a truncation
# that kept the stream (copytruncate, in-place rewrite) from one that
# replaced it — the same idea as search_writer's PRINT_SAMPLE.
_HEAD_SAMPLE = 4096

# A session the hub starts tailing with no cursor and no journal gets its
# recent history, not all of it: the phone shows the last 500 messages,
# and reading a hundred whole transcripts at startup held >1 GB.
BACKFILL_BYTES = 2 << 20
BACKFILL_MESSAGES = 500

# A decoded message carrying this key marks a usage/rate-limit hit; the
# manager pops it before the message leaves and raises ``notify`` kind
# ``limit`` from it. Its value is ``{"resets_at": epoch s, "window": ...}``
# with whatever the harness recorded (either may be missing).
LIMIT_MARK = "_limit"
# A limit record older than this when the tailer reads it (backfill of a
# session seen for the first time, a long-offline satellite) is history:
# it still shows in the chat but rings nothing.
LIMIT_FRESH_S = 600.0
# The same limit (same reset time, or the same text when none is known)
# rings once per session: again only after its reset time, or after this
# long when the harness gave none. Retrying into a limit writes a new
# record per attempt.
LIMIT_REPEAT_S = 3600.0

# Snapshot stores (no byte cursor) re-read only when something changed;
# this is how long a "nothing changed" answer is trusted before a full
# re-read anyway, in case a store writes somewhere the check can't see.
# Devin's token is exact and never expires: see ``SnapshotSource.poll``.
SNAPSHOT_RECHECK_S = 60.0
# A bounded read (Devin) returns this many newest chain nodes, so a poll
# sees a sliding window; more than a poll interval ever adds.
SNAPSHOT_WINDOW = 1000
# A Devin source keeps its window's decoded nodes between reads
# (``reports.transcripts.DevinChain``), about 1 MB for a long session;
# one whose session has not changed for this long lets them go.
DEVIN_CHAIN_KEEP_S = 300.0
# A snapshot read that found no source files (a store not written yet,
# or gone) leaves nothing to stat for a change token, and an unreadable
# read leaves no token to keep. Either way the next attempt
# waits this long, doubling per miss up to SNAPSHOT_RECHECK_S, instead of
# a full read attempt every poll (#1526).
SNAPSHOT_MISS_S = 1.0


def _rec_ts(rec: dict) -> float | None:
    """Epoch seconds from a record's timestamp field, whatever its shape."""
    for key in ("timestamp", "ts", "time", "created_at"):
        value = rec.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # Engines store ms; a value too large for seconds is ms.
            v = float(value)
            return v / 1000 if v > 1e12 else v
        if isinstance(value, str):
            from swe.sessions.engines.common import parse_iso_ts

            parsed = parse_iso_ts(value)
            if parsed:
                return parsed / 1000
    return None


def _role_kind(role: Any) -> str:
    role = str(role or "").lower()
    return "user" if role in {"user", "human"} else "assistant"


_ARG_HINT_KEYS = ("command", "cmd", "path", "file_path", "filePath", "filename")


def _summarize_args(name: str, args: Any) -> tuple[str, str]:
    """(one-line card summary, full JSON) for a tool call's arguments."""
    if isinstance(args, str):
        try:
            decoded = json.loads(args)
            if isinstance(decoded, (dict, list)):
                args = decoded
        except (json.JSONDecodeError, RecursionError):
            pass
    try:
        full = json.dumps(args, ensure_ascii=False) if args is not None else ""
    except (TypeError, ValueError):
        full = str(args)
    # args_full lands in the journal and on phones — redact like the
    # one-line summary does, or a `KEY=sk-...` Bash arg leaks verbatim.
    full = _tr._redact_secrets(full)
    if isinstance(args, dict):
        for key in _ARG_HINT_KEYS:
            value = args.get(key)
            if value:
                return f"{name} · {_tr._redact_secrets(str(value))[:160]}", full
    return f"{name}", full


def _decode_blocks(role: str, content: Any, ts: float | None) -> list[dict]:
    """Block-detail decode for ``message.content`` lists.

    Covers the Claude/pi/Cursor shape where content is a list of typed
    blocks; unknown block dicts fall back to their text member.
    """
    if isinstance(content, str):
        text = _tr._clean_text(content)
        return [protocol.message(_role_kind(role), text, ts=ts)] if text else []
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    pending_text: list[Any] = []

    def flush_text() -> None:
        # Consecutive text-ish blocks merge into one role message, emitted
        # at the position where they appeared (block order matters).
        text = _tr._clean_text(pending_text)
        pending_text.clear()
        if text:
            out.append(protocol.message(_role_kind(role), text, ts=ts))

    for block in content:
        if not isinstance(block, dict):
            pending_text.append(block)
            continue
        btype = str(block.get("type") or "").lower()
        if btype in {"thinking", "redacted_thinking", "reasoning"}:
            flush_text()
            text = _tr._clean_text(
                block.get("thinking") or block.get("text") or block.get("summary")
            )
            if text:
                out.append(protocol.message("thinking", text, ts=ts))
        elif btype in {"tool_use", "tool-call", "toolcall", "function_call"}:
            flush_text()
            name = str(block.get("name") or block.get("tool") or "tool")
            args = block.get("input") or block.get("arguments") or block.get("args")
            summary, full = _summarize_args(name, args)
            out.append(protocol.message(
                "tool_use", "",
                ts=ts,
                tool={
                    "name": name,
                    "call_id": str(block.get("id") or block.get("call_id") or ""),
                    "args_summary": summary,
                    "args_full": full,
                },
            ))
        elif btype in {
            "tool_result", "toolresult", "tool-result", "function_call_output",
        }:
            flush_text()
            body = block.get("content") or block.get("output") or block.get("text")
            text = _tr._clean_text(body)
            out.append(protocol.message(
                "tool_result", text,
                ts=ts,
                result_ref=str(
                    block.get("tool_use_id")
                    or block.get("call_id")
                    or block.get("id")
                    or ""
                ) or None,
            ))
        else:
            pending_text.append(block)
    flush_text()
    return out


def _decode_claude(rec: dict) -> list[dict]:
    ts = _rec_ts(rec)
    rtype = rec.get("type")
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    if rtype == "user":
        if rec.get("isMeta"):
            return []
        return _decode_blocks("user", msg.get("content"), ts)
    if rtype == "assistant":
        if rec.get("isApiErrorMessage"):
            text = _tr._clean_text(msg.get("content")) or "API error"
            out = protocol.message("error", text, ts=ts)
            if rec.get("error") == "rate_limit":
                # "You've hit your session limit · resets 9:30am": the
                # 429 record Claude Code writes, with ``quotaLimits``
                # (``resetsAt`` epoch s, ``rateLimitType`` five_hour,
                # seven_day, ...) on current versions.
                quota = rec.get("quotaLimits")
                quota = quota if isinstance(quota, dict) else {}
                out[LIMIT_MARK] = _limit_info(
                    quota.get("resetsAt"), quota.get("rateLimitType"))
            return [out]
        return _decode_blocks("assistant", msg.get("content"), ts)
    if rtype == "summary":
        text = _tr._clean_text(rec.get("summary"))
        return [protocol.message("compaction", text, ts=ts)] if text else []
    if rtype == "system":
        if rec.get("subtype") == "compact_boundary" or rec.get("isCompactBoundary"):
            return [protocol.message("compaction", "context compacted", ts=ts)]
        text = _tr._clean_text(rec.get("content") or rec.get("text"))
        return [protocol.message("system", text, ts=ts)] if text else []
    return []


def _limit_info(resets_at: Any, window: Any) -> dict:
    info: dict = {}
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) \
            and resets_at > 0:
        info["resets_at"] = float(resets_at)
    if isinstance(window, str) and window:
        info["window"] = window
    return info


_CODEX_TEXT_BLOCKS = {"input_text", "output_text", "text"}


def _codex_text(blocks: Any) -> str:
    parts = [
        str(b.get("text") or "")
        for b in (blocks if isinstance(blocks, list) else [])
        if isinstance(b, dict) and b.get("type") in _CODEX_TEXT_BLOCKS
    ]
    return _tr._clean_text("\n".join(p for p in parts if p))


def _decode_codex(rec: dict) -> list[dict]:
    ts = _rec_ts(rec)
    rtype = rec.get("type")
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    ptype = str(payload.get("type") or "")
    if rtype == "response_item":
        if ptype == "message":
            # Codex's own instructions (developer/system turns: skills,
            # time context, collaboration mode) are not the agent talking.
            if str(payload.get("role") or "").lower() in {"developer", "system"}:
                return []
            return [m for m in [
                protocol.message(_role_kind(payload.get("role")),
                                 _codex_text(payload.get("content")), ts=ts)
            ] if m["text"]]
        if ptype == "reasoning":
            summary = payload.get("summary") or []
            text = _tr._clean_text(
                [b.get("text") for b in summary if isinstance(b, dict)]
            ) or _codex_text(payload.get("content"))
            return [protocol.message("thinking", text, ts=ts)] if text else []
        if ptype in {"function_call", "custom_tool_call", "local_shell_call"}:
            name = str(payload.get("name") or "tool")
            args = payload.get("arguments") or payload.get("input")
            summary, full = _summarize_args(name, args)
            return [protocol.message(
                "tool_use", "",
                ts=ts,
                tool={
                    "name": name,
                    "call_id": str(payload.get("call_id") or payload.get("id") or ""),
                    "args_summary": summary,
                    "args_full": full,
                },
            )]
        if ptype in {"function_call_output", "custom_tool_call_output"}:
            return [protocol.message(
                "tool_result",
                _tr._clean_text(payload.get("output")),
                ts=ts,
                result_ref=str(payload.get("call_id") or "") or None,
            )]
        if "compact" in ptype:
            return [protocol.message("compaction", "context compacted", ts=ts)]
        return []
    if rtype == "event_msg":
        if ptype == "turn_aborted":
            return [protocol.message("system", "turn interrupted", ts=ts)]
        if ptype == "task_complete":
            # A turn that ended on a usage limit carries
            # ``error: {message, codex_error_info: "usage_limit_exceeded"}``
            # ("You hit your spend cap ..."); other turn ends are
            # bookkeeping here.
            err = payload.get("error")
            if isinstance(err, dict) \
                    and err.get("codex_error_info") == "usage_limit_exceeded":
                text = _tr._clean_text(err.get("message")) \
                    or "usage limit reached"
                out = protocol.message("error", text, ts=ts)
                out[LIMIT_MARK] = {}
                return [out]
            return []
        if "compact" in ptype:
            return [protocol.message("compaction", "context compacted", ts=ts)]
        return []
    return []


def _decode_generic(rec: dict) -> list[dict]:
    """Fallback: normalize via ``_messages_from_record`` when no block
    structure is obvious (droid, grok, freebuff and any new shape)."""
    nested = rec.get("message") if isinstance(rec.get("message"), dict) else None
    if nested is not None:
        role = nested.get("role") or rec.get("role")
        content = nested.get("content", nested.get("text"))
        if isinstance(content, list):
            got = _decode_blocks(role, content, _rec_ts(rec))
            if got:
                return got
    out: list[dict] = []
    for msg in _tr._messages_from_record(rec):
        out.append(_normalized_to_payload(msg))
    return out


def _normalized_to_payload(msg) -> dict:
    """One ``NormalizedMessage`` → a RemoteMessage payload."""
    if msg.role == "tool":
        label, _, _sep = msg.text.partition(":")
        name = label.strip()[:40] or "tool"
        return protocol.message(
            "tool_use", "",
            ts=msg.timestamp,
            tool={
                "name": name,
                "call_id": "",
                "args_summary": msg.text[:160],
                "args_full": msg.text[:8000],
            },
        )
    return protocol.message(
        "user" if msg.role == "human" else "assistant",
        msg.text, ts=msg.timestamp,
    )


_DECODERS: dict[str, Callable[[dict], list[dict]]] = {
    "claude": _decode_claude,
    "codex": _decode_codex,
}


def decode_record(tool_name: str, record: dict) -> list[dict]:
    """One transcript record → zero or more RemoteMessage payloads."""
    try:
        decoder = _DECODERS.get(tool_name, _decode_generic)
        return injected.clean_all(decoder(record))
    except Exception:
        return []


# ── thinking durations ───────────────────────────────────────────────────

# Harnesses that write each content block as its own timestamped record
# the moment the block finishes: a thinking record's timestamp is when
# the thinking ended, and the record before it (the prompt, a tool
# result, Claude's api-request) is when it began. Harnesses that write a
# whole reply as one record (pi, droid, ...) give no such split.
_BLOCK_RECORD_TOOLS = frozenset({"claude", "codex"})

# Codex event_msg records that shadow a reasoning item and land just
# before it: they mark the reasoning's end, not the start of anything.
_CODEX_REASONING_EVENTS = frozenset({
    "agent_reasoning", "agent_reasoning_raw_content",
    "agent_reasoning_section_break", "agent_reasoning_delta",
    "reasoning_content_delta", "reasoning_raw_content_delta",
})


def _duration(start: Any, end: Any) -> float | None:
    """``end - start`` rounded to 0.1, or None when either is not a
    number or the span runs backwards."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in (start, end)):
        return None
    span = float(end) - float(start)
    return round(span, 1) if span >= 0 else None


def _thinking(text: str, ts: float, duration: float | None) -> dict:
    """A thinking message, timed when the store recorded its span."""
    msg = protocol.message("thinking", text, ts=ts)
    if duration is not None:
        msg["duration_s"] = duration
    return msg


def _span_ms(times: Any) -> float | None:
    """An opencode ``time`` dict's span in seconds: legacy parts carry
    ``{start, end}``, 2.x content blocks ``{created, completed}`` (ms)."""
    if not isinstance(times, dict):
        return None
    for lo, hi in (("start", "end"), ("created", "completed")):
        if times.get(lo) is not None and times.get(hi) is not None:
            span = _duration(times.get(lo), times.get(hi))
            return None if span is None else round(span / 1000, 1)
    return None


def _empty_thinking(tool_name: str, rec: dict) -> bool:
    """A thinking record with nothing to show: Claude Code writes an
    empty thinking block a moment before the full one, and Codex an
    encrypted reasoning item with no summary."""
    if tool_name == "claude" and rec.get("type") == "assistant":
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        blocks = msg.get("content")
        return isinstance(blocks, list) and bool(blocks) and all(
            isinstance(b, dict)
            and b.get("type") in {"thinking", "redacted_thinking"}
            for b in blocks
        )
    if tool_name == "codex" and rec.get("type") == "response_item":
        payload = rec.get("payload")
        return isinstance(payload, dict) and payload.get("type") == "reasoning"
    return False


def _thinking_companion(tool_name: str, rec: dict) -> bool:
    """A record that belongs to a reasoning item rather than preceding
    it, so it must not move the thinking start mark."""
    if tool_name != "codex" or rec.get("type") != "event_msg":
        return False
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    ptype = str(payload.get("type") or "")
    if ptype in _CODEX_REASONING_EVENTS:
        return True
    item = payload.get("item")
    return ptype == "item_completed" and isinstance(item, dict) \
        and str(item.get("type") or "").lower() == "reasoning"


def stamp_thinking(
    tool_name: str, rec: dict, messages: list[dict], since: float | None,
) -> float | None:
    """Set ``duration_s`` on a thinking-only record's messages and
    return the start mark for the next record.

    ``since`` is the timestamp of the last record before this one. Only
    a record that decodes to thinking alone is stamped: a record mixing
    thinking with text was written when the whole reply ended, so its
    gap would count the reply too. An empty thinking record leaves the
    mark where it was, so the full one after it is timed from the start.
    An untimed record that shows something (a prompt, a tool result)
    clears it: the thinking after it started at an unknown time. Untimed
    bookkeeping records that show nothing (Claude's titles, agent names,
    request blobs) leave it.
    """
    if tool_name not in _BLOCK_RECORD_TOOLS:
        return since
    ts = _rec_ts(rec)
    if ts is None:
        return None if messages else since
    if _thinking_companion(tool_name, rec) \
            or (not messages and _empty_thinking(tool_name, rec)):
        return since
    if since is not None and messages \
            and all(m.get("kind") == "thinking" for m in messages):
        span = _duration(since, ts)
        if span is not None:
            for m in messages:
                m["duration_s"] = span
    return ts


# ── transcript file resolution ────────────────────────────────────────────

# Roots mirror transcripts.py's reader registry; ``_find_session_file``
# resolves ``session_id`` to a file under each (stem or path-part match).
_JSONL_ROOTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "claude": ("~/.claude/projects", ("*.jsonl",)),
    "droid": ("~/.factory/sessions", ("*.jsonl",)),
    "codex": ("~/.codex/sessions", ("*.jsonl",)),
    "pi": ("~/.pi/agent/sessions", ("*.jsonl",)),
    "cursor": ("~/.cursor/projects", ("*.jsonl",)),
    "grok": ("~/.grok/sessions", ("chat_history.jsonl",)),
    "freebuff": ("~/.config/manicode/projects", ("log.jsonl",)),
}

# The transcript subdir each account-aware harness keeps inside its home,
# so a session in an SWE account home resolves there, not the default.
# Codex is absent: its account homes resolve through `_codex_rollout_path`.
_ACCOUNT_SUBDIRS = {"claude": "projects"}

_OPENCODE_DBS = {
    "opencode": "~/.local/share/opencode/opencode.db",
    "kilo": "~/.local/share/kilo/kilo.db",
}


def resolve_jsonl_path(session: Session) -> Path | None:
    entry = _JSONL_ROOTS.get(session.tool_name)
    if entry is None:
        return None
    if session.session_id:
        direct = expand_user(session.session_id)
        if direct.is_file():
            return direct
    if session.tool_name == "codex":
        # `_codex_rollout_path` is the one rollout resolver: it finds the
        # dated path and the flat spot archiving moved the file to.
        from swe.sessions.status import _codex_rollout_path

        found = _codex_rollout_path(session.session_id, session.account_home)
        return Path(found) if found else None
    root, patterns = entry
    root = expand_user(root)
    subdir = _ACCOUNT_SUBDIRS.get(session.tool_name)
    if subdir and session.account_home:
        root = Path(session.account_home) / subdir
    if session.tool_name == "claude" and session.session_id:
        # Resume-from-another-cwd leaves one copy per project dir; the
        # newest write is live (same rule as sessions/status.py).
        hits = glob.glob(
            os.path.join(str(root), "*", session.session_id + ".jsonl")
        )
        if hits:
            hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            return Path(hits[0])
        return None
    return _tr._find_session_file(root, session.session_id, patterns)


# ── sources ──────────────────────────────────────────────────────────────


class JsonlTailSource:
    """Incremental byte-offset reader over one JSONL transcript."""

    def __init__(self, path: Path, tool_name: str) -> None:
        self.path = path
        self.tool_name = tool_name
        self.offset = 0
        # Set by start_near_end: the first read begins mid-line, so the
        # partial line up to the first newline is dropped.
        self._align = False
        # The file's first bytes, kept so a shrink can be told apart: a
        # copytruncate (or an in-place rewrite) keeps this head, a
        # rotated or rewritten file does not.
        self._head = b""
        # Timestamp of the last record read: the start of a thinking
        # record that follows it (see stamp_thinking). Kept in the
        # cursor so a hub restart between a prompt and its thinking
        # still times it.
        self._since: float | None = None

    def state(self) -> dict:
        return {
            "source": "jsonl",
            "path": str(self.path),
            "offset": self.offset,
            "since": self._since,
            # The head sample itself rides in the cursor: hashing alone
            # can only verify a file that still reaches head_len, but a
            # transcript may have been cut *shorter* than that while the
            # hub was down — then every remaining byte needs comparing
            # against the stored prefix. head_len/head_sha stay so a
            # cursor written here still verifies on the previous build.
            "head": (
                base64.b64encode(self._head).decode("ascii")
                if self._head else ""
            ),
            "head_len": len(self._head),
            "head_sha": (
                hashlib.sha256(self._head).hexdigest() if self._head else ""
            ),
        }

    def restore(self, state: dict) -> bool:
        """Resume from a saved cursor; False when it isn't this file's
        (another path, or a head that no longer matches), leaving the
        source unplaced for the caller to start fresh."""
        if state.get("source") != "jsonl" or state.get("path") != str(self.path):
            return False
        try:
            offset = int(state.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        since = state.get("since")
        # Taken only once the cursor proves to be this file's: a mark
        # from a replaced file would time a thinking block from it.
        since = float(since) if isinstance(since, (int, float)) \
            and not isinstance(since, bool) else None
        stored = state.get("head")
        if isinstance(stored, str) and stored:
            try:
                head = base64.b64decode(stored.encode("ascii"))
            except ValueError:
                head = b""
            if head:
                cur = self._file_head(len(head))
                if cur != head[: len(cur)]:
                    # Not the file the cursor tracked: a rewrite that
                    # happened while down.
                    return False
                # Verified — possibly a prefix shorter than the sample,
                # when the file was truncated while down.
                self.offset, self._head, self._since = offset, head, since
                return True
        # A cursor written before the bytes were stored verifies only a
        # file that still reaches the sampled length.
        try:
            head_len = int(state.get("head_len") or 0)
        except (TypeError, ValueError):
            head_len = 0
        head_sha = str(state.get("head_sha") or "")
        if not (head_len and head_sha):
            # A cursor from before the check: nothing to verify.
            self.offset, self._since = offset, since
            return True
        head = self._file_head(head_len)
        if len(head) != head_len or hashlib.sha256(head).hexdigest() != head_sha:
            return False
        self.offset, self._head, self._since = offset, head, since
        return True

    def skip_to_end(self) -> None:
        self._since = None
        try:
            self.offset = self.path.stat().st_size
        except OSError:
            pass
        self._prime_head()

    def start_near_end(self, max_bytes: int | None = None) -> None:
        """Backfill only the last ``max_bytes`` (default BACKFILL_BYTES)."""
        max_bytes = BACKFILL_BYTES if max_bytes is None else max_bytes
        self._since = None
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        self._prime_head()
        if size > max_bytes:
            self.offset = size - max_bytes
            self._align = True

    def continue_from(self, old: "JsonlTailSource") -> bool:
        """Place this source right after the last line ``old`` read,
        found in this file; False (unplaced) when it isn't here.

        Claude ``--resume`` from another folder copies the transcript
        line for line into a new file, then appends the new turns. The
        catalog sees the new file a refresh later, so turns can already
        sit past the copy: starting at the end would lose them.

        The boundary is the last ``_BOUNDARY_BYTES`` before ``old``'s
        offset: several lines of context, so one repeated short line
        can't match early, and bounded however large the last record
        is. It is looked for at the same offset first (a real resume
        copies byte for byte), then within ``_HANDOFF_SPAN`` of it.
        """
        end = old.offset
        if old._align or end <= 0:
            return False  # never polled, or nothing read
        start = max(0, end - _BOUNDARY_BYTES)
        try:
            with open(old.path, "rb") as fh:
                fh.seek(start)
                mark = fh.read(end - start)
        except OSError:
            return False
        if len(mark) != end - start or not mark.endswith(b"\n"):
            return False
        lo = max(0, start - _HANDOFF_SPAN)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(lo)
                window = fh.read(start - lo + _HANDOFF_SPAN + len(mark))
        except OSError:
            return False
        if window[start - lo:end - lo] == mark:
            at = start
        else:
            found = window.find(mark)
            if found < 0:
                return False
            at = lo + found
        self.offset = at + len(mark)
        self._since = old._since
        self._prime_head()
        return True

    def _file_head(self, span: int) -> bytes:
        try:
            with open(self.path, "rb") as fh:
                return fh.read(span)
        except OSError:
            return b""

    def _prime_head(self) -> None:
        """Snapshot the file's head for the truncation check."""
        self._head = self._file_head(_HEAD_SAMPLE)

    def _resume_truncated(self, size: int) -> bool:
        """The file shrank below the cursor: pick where to continue.

        A head still on disk means the stream was cut in place
        (logrotate's copytruncate, an in-place rewrite that kept its
        start): everything left on disk is a prefix of what was
        already sent, so resume at the cut — the next read sees only
        bytes appended after it, and nothing replays. Resuming inside
        the file instead (right after the sample) would replay
        retained records as new and could land mid-record.

        Only the first ``_HEAD_SAMPLE`` bytes are verifiable, so a
        prefix longer than that is trusted on the head's say-so: a
        rewrite could hide new content between the sample and the
        cut, but replaying retained records is the worse failure for
        a chat tail. A changed head is a new stream (a rotated or
        rewritten file): restart from 0. Returns False when nothing
        readable is left.
        """
        # Either way the read begins at a file position that owns the
        # next bytes written: nothing to align.
        self._align = False
        span = min(size, len(self._head))
        if span and self._head[:span] == self._file_head(span):
            self.offset = size
            return False
        self.offset = 0
        self._head = b""
        self._since = None  # a new stream: its first record has no start
        return True

    def poll(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size <= self.offset:
            if size < self.offset:  # truncated/rotated
                if not self._resume_truncated(size):
                    return []
            else:
                return []
        try:
            start = self.offset
            with open(self.path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(min(size - start, _MAX_CHUNK))
        except OSError:
            return []
        if start == 0:
            # A from-scratch read refreshes the head fingerprint (the
            # file may have been replaced since the last one).
            self._head = chunk[:_HEAD_SAMPLE]
        if self._align:
            # Mid-line start: drop through the first newline.
            first_nl = chunk.find(b"\n")
            if first_nl < 0:
                if len(chunk) >= _MAX_CHUNK:
                    # Still inside one huge record: step past this chunk
                    # and keep looking, rather than re-reading it forever.
                    self.offset += len(chunk)
                return []
            self.offset += first_nl + 1
            chunk = chunk[first_nl + 1:]
            self._align = False
        # offset only advances past complete lines; an incomplete tail line
        # is re-read from the same offset on the next poll.
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            if size - self.offset <= _MAX_CHUNK:
                return []  # writer still mid-line; wait for the rest
            # One record bigger than the chunk cap: consume through its
            # newline so it can't wedge every later message forever.
            self.offset = self._skip_oversized()
            return [protocol.message(
                "system",
                "[skipped an oversized transcript record]",
            )]
        self.offset += last_nl + 1
        out: list[dict] = []
        for raw in chunk[:last_nl].split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                continue
            if isinstance(rec, dict):
                got = decode_record(self.tool_name, rec)
                self._since = stamp_thinking(
                    self.tool_name, rec, got, self._since)
                out.extend(got)
        return out

    def _skip_oversized(self) -> int:
        """Advance past a record larger than the chunk cap.

        Scans forward from the current offset to the next newline so a
        multi-MB single line can't starve everything appended after it.
        """
        offset = self.offset + _MAX_CHUNK
        size = offset
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as fh:
                while offset < size:
                    fh.seek(offset)
                    piece = fh.read(_MAX_CHUNK)
                    idx = piece.find(b"\n")
                    if idx >= 0:
                        return offset + idx + 1
                    offset += len(piece)
                    if not piece:
                        break
        except OSError:
            pass
        return size


class MirrorCopySource(JsonlTailSource):
    """The chat of a session whose harness deleted its transcript, read
    from swe's mirror copy (``sessions/transcript_mirror.py``).

    Read-only: it reads what the copy held when the source was made and
    then nothing more, so nothing is ever tailed from it, and the session
    has no resume flags, so no writer attaches. A restored session moves
    to its live file through ``TailManager._reresolve``, continuing after
    the last line this source read."""

    read_only = True

    def __init__(self, path: Path, tool_name: str) -> None:
        super().__init__(path, tool_name)
        try:
            self._end = path.stat().st_size
        except OSError:
            self._end = 0

    def poll(self) -> list[dict]:
        if self.offset >= self._end:
            return []
        return super().poll()


def mirror_copy_path(session: Session) -> Path | None:
    """The mirror copy a read of ``session`` falls back to, by the rule
    ``reports.transcripts.read_transcript`` uses (``find_copy``). Only for
    reads: callers asking about the live transcript (attach, adoption,
    activity times) use ``resolve_jsonl_path``, which never returns a
    copy."""
    from swe.sessions import transcript_mirror

    if session.tool_name not in transcript_mirror.MIRRORED:
        return None
    return transcript_mirror.find_copy(
        session.tool_name, session.session_id, session.account_home)


def _completed(data: dict) -> bool:
    """Whether an opencode 2.x assistant row has finished streaming."""
    times = data.get("time")
    return isinstance(times, dict) and bool(times.get("completed"))


def _part_cursor(value) -> tuple[int, str]:
    """Deserialize a legacy ``part`` cursor.

    Today's ``[time_created, id]`` pair, or a bare ``time_created`` int
    written before the same-millisecond fix — that one resumes at
    ``(time, "")`` so a part that shared the cursor's millisecond but
    was never read is picked up (one re-read of already-seen rows at
    that ms is the price).
    """
    if isinstance(value, (list, tuple)) and value:
        try:
            created = int(value[0])
            part_id = str(value[1] if len(value) > 1 else "")
            return created, part_id
        except (TypeError, ValueError):
            return (0, "")
    try:
        return int(value or 0), ""
    except (TypeError, ValueError):
        return (0, "")


class OpencodeSource:
    """Incremental poll over opencode/kilo's drizzle ``part`` table, and
    the ``session_message`` rows opencode 2.x writes instead.

    The two stores keep separate cursors: ``cursor`` is the
    ``(time_created, id)`` position of the last ``part`` row consumed —
    several parts can share a millisecond, so a bare timestamp would
    skip the ones sorted after the first row seen at it — and ``seq``
    the last v2 row emitted. A v2 assistant row is written when its
    reply starts and rewritten as it streams, so the v2 cursor stops
    before the first assistant row with no ``time.completed`` and reads
    it again next poll.
    """

    def __init__(self, db_path: Path, session_id: str) -> None:
        self.db_path = db_path
        self.session_id = session_id
        self.cursor: tuple[int, str] = (0, "")
        self.seq: int | None = None  # last v2 row emitted; None = not placed yet
        self._roles: dict[str, str] = {}

    def state(self) -> dict:
        return {
            "source": "opencode",
            "session_id": self.session_id,
            "cursor": [self.cursor[0], self.cursor[1]],
            "seq": self.seq,
        }

    def restore(self, state: dict) -> bool:
        """Resume from a saved cursor; False when it is another
        session's or another source's."""
        if state.get("source") != "opencode" or \
                state.get("session_id", self.session_id) != self.session_id:
            return False
        self.cursor = _part_cursor(state.get("cursor"))
        try:
            seq = state.get("seq")
            self.seq = None if seq is None else int(seq)
        except (TypeError, ValueError):
            self.seq = None
        return True

    def skip_to_end(self) -> None:
        conn = self._open()
        if conn is None:
            return
        try:
            row = conn.execute(
                "SELECT MAX(time_created) FROM part WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
            if row and row[0]:
                # Past every row at that millisecond, whatever id a
                # straggler written later happens to carry.
                self.cursor = (int(row[0]) + 1, "")
        except sqlite3.Error:
            pass
        try:
            self.seq = self._settled_seq(opencode_store.v2_rows(conn, self.session_id), -1)
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def start_near_end(self, max_messages: int | None = None) -> None:
        """Place both cursors so the first poll reads only the newest
        ``max_messages`` (default BACKFILL_MESSAGES) rows of each store,
        not the session's whole history."""
        cap = BACKFILL_MESSAGES if max_messages is None else max_messages
        conn = self._open()
        if conn is None:
            return
        try:
            row = conn.execute(
                "SELECT time_created, id FROM part WHERE session_id = ? "
                "ORDER BY time_created DESC, id DESC LIMIT 1 OFFSET ?",
                (self.session_id, cap),
            ).fetchone()
            if row:
                # The newest row left out: the poll reads strictly after.
                self.cursor = (int(row[0]), str(row[1]))
        except sqlite3.Error:
            pass
        try:
            # The boundary comes from the raw rows: v2_rows drops a row
            # whose JSON won't decode, which would shorten the window
            # and read as a short session (the whole history again).
            row = None
            if opencode_store.has_v2_messages(conn):
                row = conn.execute(
                    "SELECT sm.seq FROM session_message sm "
                    "WHERE sm.session_id = ? "
                    f"{opencode_store._not_legacy(conn)} "
                    "ORDER BY sm.seq DESC LIMIT 1 OFFSET ?",
                    (self.session_id, cap),
                ).fetchone()
            self.seq = int(row[0] or 0) if row else -1
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    @staticmethod
    def _settled_seq(rows: list, seq: int) -> int:
        """The last seq before the first assistant row still streaming:
        that row is rewritten in place when it finishes, so a cursor past
        it would never read the finished reply."""
        for _id, kind, row_seq, _created, data in rows:
            if kind == "assistant" and not _completed(data):
                break
            seq = row_seq
        return seq

    def _place_seq(self, conn: sqlite3.Connection) -> int:
        """The v2 cursor for a source restored from a cursor file written
        before it existed: every v2 row at or before the ``part`` cursor's
        time counts as already sent, so an upgrade replays nothing."""
        if self.seq is None:
            self.seq = -1  # seq starts at 0
            if self.cursor[0]:
                sent = [r for r in opencode_store.v2_rows(conn, self.session_id) if r[3] <= self.cursor[0]]
                self.seq = self._settled_seq(sent, -1)
        return self.seq

    def _open(self) -> sqlite3.Connection | None:
        try:
            # as_uri() percent-encodes the path: a raw "?" or "#" in a
            # home folder name would end the file: URI early.
            return sqlite3.connect(
                Path(self.db_path).absolute().as_uri() + "?mode=ro", uri=True
            )
        except (sqlite3.Error, ValueError):
            return None

    def _role_for(self, conn: sqlite3.Connection, message_id: str) -> str:
        role = self._roles.get(message_id)
        if role is not None:
            return role
        # sqlite3.Error propagates to poll(): the row keeps its place
        # under the cursor and is retried next poll. Catching it here
        # and caching the fallback would pin a user message as
        # "assistant" forever.
        row = conn.execute(
            "SELECT data FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            # The message row can commit after the part that references
            # it; answer the fallback but do not cache it, so a later
            # poll still resolves the real role.
            return "assistant"
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError, RecursionError):
            data = None
        role = _role_kind(data.get("role")) if isinstance(data, dict) else ""
        role = role or "assistant"
        self._roles[message_id] = role
        return role

    def poll(self) -> list[dict]:
        conn = self._open()
        if conn is None:
            return []
        out: list[dict] = []
        try:
            rows = conn.execute(
                "SELECT id, message_id, time_created, data FROM part "
                "WHERE session_id = ? AND (time_created > ? "
                "OR (time_created = ? AND id > ?)) "
                "ORDER BY time_created, id",
                (self.session_id, self.cursor[0],
                 self.cursor[0], self.cursor[1]),
            ).fetchall()
            for part_id, message_id, created, raw in rows:
                try:
                    part = json.loads(raw)
                except (TypeError, ValueError, RecursionError):
                    part = None
                if isinstance(part, dict):
                    self._decode_part(
                        conn, out, str(part_id), str(message_id),
                        int(created), part,
                    )
                # The cursor lands only on rows already consumed: an
                # error mid-row keeps it here so the row is re-read next
                # poll instead of skipped forever.
                self.cursor = (int(created), str(part_id))
            out.extend(self._poll_v2(conn))
        except sqlite3.Error:
            return injected.clean_all(out)
        finally:
            conn.close()
        return injected.clean_all(out)

    def _decode_part(
        self,
        conn: sqlite3.Connection,
        out: list[dict],
        part_id: str,
        message_id: str,
        created: int,
        part: dict,
    ) -> None:
        """Append one ``part`` row's events to ``out``."""
        ts = float(created) / 1000
        ptype = str(part.get("type") or "").lower()
        role = self._role_for(conn, str(message_id))
        if ptype == "text":
            text = _tr._clean_text(part.get("text"))
            if text:
                out.append(protocol.message(_role_kind(role), text, ts=ts))
        elif ptype in {"reasoning", "thinking"}:
            text = _tr._clean_text(part.get("text"))
            if text:
                out.append(_thinking(text, ts, _span_ms(part.get("time"))))
        elif ptype in {"tool", "tool-invocation", "tool_result"}:
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            name = str(part.get("tool") or "tool")
            call_id = str(part.get("id") or part_id or "")
            summary, full = _summarize_args(name, state.get("input"))
            out.append(protocol.message(
                "tool_use", "",
                ts=ts,
                tool={
                    "name": name,
                    "call_id": call_id,
                    "args_summary": summary,
                    "args_full": full,
                },
            ))
            status = str(state.get("status") or "")
            if status in {"completed", "error"} or state.get("output"):
                out.append(protocol.message(
                    "tool_result",
                    _tr._clean_text(state.get("output")),
                    ts=ts,
                    result_ref=call_id or None,
                ))
        elif "compact" in ptype:
            out.append(protocol.message(
                "compaction", "context compacted", ts=ts))

    def _poll_v2(self, conn: sqlite3.Connection) -> list[dict]:
        out: list[dict] = []
        after = self._place_seq(conn)
        for _id, kind, seq, created, data in opencode_store.v2_rows(
            conn, self.session_id, after_seq=after
        ):
            ts = float(created) / 1000
            if kind in opencode_store.PROMPT_TYPES:
                text = _tr._clean_text(data.get("text"))
                if text:
                    out.append(protocol.message("user", text, ts=ts))
            elif kind == "assistant":
                if not _completed(data):
                    break  # still streaming: read it again next poll
                out.extend(self._v2_assistant(data, ts))
            elif kind == "compaction":
                out.append(protocol.message(
                    "compaction", "context compacted", ts=ts))
            self.seq = seq
        return out

    def _v2_assistant(self, data: dict, ts: float) -> list[dict]:
        out: list[dict] = []
        for block in opencode_store.content_blocks(data):
            btype = str(block.get("type") or "").lower()
            if btype == "text":
                text = _tr._clean_text(block.get("text"))
                if text:
                    out.append(protocol.message("assistant", text, ts=ts))
            elif btype in {"reasoning", "thinking"}:
                text = _tr._clean_text(block.get("text"))
                if text:
                    out.append(
                        _thinking(text, ts, _span_ms(block.get("time"))))
            elif btype == "tool":
                state = block.get("state")
                state = state if isinstance(state, dict) else {}
                name = str(block.get("name") or "tool")
                call_id = str(block.get("id") or "")
                summary, full = _summarize_args(name, state.get("input"))
                out.append(protocol.message(
                    "tool_use", "",
                    ts=ts,
                    tool={
                        "name": name,
                        "call_id": call_id,
                        "args_summary": summary,
                        "args_full": full,
                    },
                ))
                if str(state.get("status") or "") in {"completed", "error"}:
                    out.append(protocol.message(
                        "tool_result",
                        _tr._clean_text(state.get("content") or state.get("error")),
                        ts=ts,
                        result_ref=call_id or None,
                    ))
        return out


class SnapshotSource:
    """Diff ``read_transcript`` output by message fingerprint.

    For stores without a byte cursor (json files, sqlite schemas we do
    not track per-record), the whole normalized stream is re-read and
    new tail items emitted. Cheap enough at the idle poll cadence.
    """

    # A changed snapshot store is re-read whole (Devin: up to
    # SNAPSHOT_WINDOW chain nodes), so a hot window polls these no
    # faster than an active session already does (issue #515).
    hot_poll_s = POLL_ACTIVE_S

    def __init__(self, session: Session) -> None:
        self.session = session
        self._fps: list[str] = []
        self._sources: list[str] = []
        self._token: tuple | None = None
        self._token_at = 0.0
        # A read that left no source files to stat: when the next may
        # run, and how long the wait after it is (see SNAPSHOT_MISS_S).
        self._miss_until = float("-inf")
        self._miss_wait = 0.0
        # Set by start_near_end: the first poll emits only the newest
        # BACKFILL_MESSAGES instead of the whole history.
        self._backfill_cap: int | None = None
        # Devin: the window kept between reads, so a changed session
        # costs the new nodes, not the whole window (#1522).
        self._chain = _tr.DevinChain() if session.tool_name == "devin" else None

    def state(self) -> dict:
        return {"source": "snapshot", "fps": self._fps}

    def restore(self, state: dict) -> bool:
        """Resume from a saved cursor; False when it isn't a snapshot
        cursor."""
        fps = state.get("fps")
        if state.get("source") != "snapshot" or not isinstance(fps, list):
            return False
        self._fps = [str(f) for f in fps]
        return True

    def _read(self):
        return _tr.read_transcript_tail(
            self.session, SNAPSHOT_WINDOW, chain=self._chain)

    def skip_to_end(self) -> None:
        try:
            tr = self._read()
        except Exception:
            return
        self._fps = [self._fp(m) for m in tr.messages]

    def start_near_end(self, max_messages: int | None = None) -> None:
        self._backfill_cap = BACKFILL_MESSAGES if max_messages is None else max_messages

    def change_token(self) -> tuple | None:
        """A cheap stand-in for "did this session change", or None when
        there is nothing to check yet (the first read).

        Devin keeps every session in one shared SQLite file, so its token
        is that session's newest message row and chain head (an indexed
        query). Other stores use the stat of the files the last read
        listed, their ``-wal`` files and their folders, so a write, a
        checkpoint or a new file each change it.
        """
        if self.session.tool_name == "devin":
            return _devin_token(self.session.session_id)
        if not self._sources:
            return None
        parts: list[tuple] = []
        for src in self._sources:
            for path in (src, src + "-wal", os.path.dirname(src)):
                try:
                    st = os.stat(path)
                    parts.append((path, st.st_mtime_ns, st.st_size))
                except OSError:
                    parts.append((path, None, None))
        return tuple(parts)

    @staticmethod
    def _fp(msg) -> str:
        return hashlib.sha1(
            f"{msg.role}\x00{msg.kind}\x00{msg.text}".encode("utf-8")
        ).hexdigest()[:16]

    def poll(self) -> list[dict]:
        now = time.monotonic()
        token = self.change_token()
        # Devin's token reads the rows the transcript read does. Devin
        # writes a message with INSERT OR REPLACE, which gives even a
        # rewritten row a new row_id, so an unchanged token means nothing
        # to read: no timed re-read of up to 1,000 nodes per idle session.
        exact = self.session.tool_name == "devin"
        if (token is not None and token == self._token
                and (exact or now - self._token_at < SNAPSHOT_RECHECK_S)):
            if self._chain is not None \
                    and now - self._token_at >= DEVIN_CHAIN_KEEP_S:
                self._chain.reset()
            return []
        if not exact and now < self._miss_until:
            # The last read found no files, or a store that couldn't be
            # read (a SQLite store that isn't there still lists its path).
            return []
        try:
            tr = self._read()
        except Exception:
            tr = None
        self._sources = list(getattr(tr, "source_paths", None) or [])
        if self._sources and tr is not None and getattr(tr, "readable", True):
            self._miss_wait = 0.0
            self._miss_until = float("-inf")
        else:
            self._miss_wait = min(SNAPSHOT_RECHECK_S,
                                  max(SNAPSHOT_MISS_S, self._miss_wait * 2))
            self._miss_until = now + self._miss_wait
        if tr is None or not getattr(tr, "readable", True):
            # A locked or missing store: keep the old token and window so
            # the next poll retries, and recovery doesn't re-emit history.
            return []
        # The token from before the read is the one kept: a write that
        # lands mid-read changes the next token, so it is picked up then.
        self._token = token if token is not None else self.change_token()
        self._token_at = now
        fps = [self._fp(m) for m in tr.messages]
        new = tr.messages[_new_from(self._fps, fps):]
        if self._backfill_cap is not None:
            new = new[-self._backfill_cap:]
            self._backfill_cap = None
        out = injected.clean_all([_normalized_to_payload(msg) for msg in new])
        self._fps = fps
        return out


# Where the Devin CLI keeps every session (read by reports.transcripts too).
DEVIN_DB = "~/.local/share/devin/cli/sessions.db"


def _new_from(old: list[str], new: list[str]) -> int:
    """Index in ``new`` where messages not seen before start.

    A full read grows at the end, so the shared prefix is the answer (a
    rewrite re-emits from the fork). A bounded read slides: the old
    window's tail reappears at the head of the new one, so the longest
    such overlap is the answer. No overlap at all means everything is new.
    """
    common = 0
    for a, b in zip(old, new):
        if a != b:
            break
        common += 1
    if common == len(old) or not old or not new:
        return common
    # Fingerprints are content, so a repeated message can make a short
    # prefix match by chance after the window slid ([a,b,a,c] then
    # [a,c,d,e]). The longest slide overlap is checked too, and the
    # larger answer wins. Only positions holding the new window's first
    # message can start an overlap; the earliest is the longest.
    overlap = 0
    first = new[0]
    for i, fp in enumerate(old):
        if fp == first and old[i:] == new[: len(old) - i]:
            overlap = len(old) - i
            break
    return max(common, overlap)


# A session's cached Devin token goes once nothing asked for it this
# long (its tailer was dropped); the next ask reads it afresh.
DEVIN_TOKEN_IDLE_S = 600.0


class _DevinTokens:
    """Every Devin tailer's change token, from one read-only connection.

    A connection per call cost a connect plus ``MAX(row_id), COUNT(*)``
    over the session's index entries: 14 ms a call for a 52K-node session
    on a 9.8 GB ``sessions.db`` (#1521). Now:

    - ``PRAGMA data_version`` on the kept connection changes only when
      another connection commits (any write, delete or update). While it
      holds still, every session's last token stands and no query runs.
    - ``MAX(row_id)`` and the chain head are read on every change. Devin
      writes a message with ``INSERT OR REPLACE``, which takes a new
      row_id, so every write moves the max at once.
    - ``COUNT(*)`` is what sees a delete below the max. It is read again
      only once SNAPSHOT_RECHECK_S has passed for that session, so a
      delete shows within a minute instead of costing a scan per poll.
      A delete takes nothing back from a phone that already has the
      message, so the delay changes nothing it shows.

    The file's inode is checked on every call: a ``sessions.db`` that
    was replaced or deleted gets a fresh connection, or None.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ident: tuple | None = None
        # session_id -> (token, data_version it was read at, COUNT(*),
        # the data_version and clock time the count was read at)
        self._cache: dict[str, tuple] = {}
        # session_id -> when it was last asked for; a session no tailer
        # asked about for DEVIN_TOKEN_IDLE_S leaves the cache.
        self._used: dict[str, float] = {}
        self._pruned_at = float("-inf")

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._ident = None
        self._cache.clear()
        self._used.clear()

    def _connect(self) -> sqlite3.Connection | None:
        path = Path(os.path.expanduser(DEVIN_DB)).absolute()
        try:
            st = os.stat(path)
        except OSError:
            self._close()
            return None
        ident = (str(path), st.st_dev, st.st_ino)
        if self._conn is not None and ident == self._ident:
            return self._conn
        self._close()
        try:
            self._conn = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, timeout=1.0,
                check_same_thread=False)
        except (sqlite3.Error, ValueError):
            return None
        self._ident = ident
        return self._conn

    def token(self, session_id: str) -> tuple | None:
        with self._lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                return self._token(conn, session_id)
            except sqlite3.Error:
                self._close()
                return None

    def _token(self, conn: sqlite3.Connection, session_id: str) -> tuple:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        now = self._clock()
        self._used[session_id] = now
        if now - self._pruned_at >= DEVIN_TOKEN_IDLE_S:
            self._pruned_at = now
            for sid in [k for k, at in self._used.items()
                        if now - at >= DEVIN_TOKEN_IDLE_S]:
                self._used.pop(sid, None)
                self._cache.pop(sid, None)
        cached = self._cache.get(session_id)
        if cached is not None:
            token, token_version, count, count_version, at = cached
            # The count is current when nothing committed since it was
            # read, or still inside its window.
            count_ok = count_version == version \
                or now - at < SNAPSHOT_RECHECK_S
            if token_version == version and count_ok:
                return token
        newest = conn.execute(
            "SELECT MAX(row_id) FROM message_nodes WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        head = conn.execute(
            "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if cached is None or not count_ok:
            count = conn.execute(
                "SELECT COUNT(*) FROM message_nodes WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            count_version, at = version, now
        token = ((newest[0] if newest else None, count), tuple(head or ()))
        self._cache[session_id] = (token, version, count, count_version, at)
        return token


_DEVIN_TOKENS = _DevinTokens()


def _devin_token(session_id: str) -> tuple | None:
    """Devin's newest message row, row count and chain head for one
    session; see ``_DevinTokens``."""
    return _DEVIN_TOKENS.token(session_id)


def make_source(session: Session) -> "JsonlTailSource | OpencodeSource | SnapshotSource | None":
    """A live source for ``session``, or None when nothing is readable.
    A transcript the harness deleted reads from swe's mirror copy,
    read-only (``MirrorCopySource``)."""
    path = resolve_jsonl_path(session)
    if path is not None:
        return JsonlTailSource(path, session.tool_name)
    copy = mirror_copy_path(session)
    if copy is not None:
        return MirrorCopySource(copy, session.tool_name)
    db = _OPENCODE_DBS.get(session.tool_name)
    if db is not None and session.session_id:
        db_path = expand_user(db)
        if db_path.exists():
            return OpencodeSource(db_path, session.session_id)
    if session.tool_name in _tr.READER_REGISTRY:
        return SnapshotSource(session)
    return None


# ── cursor persistence + the poll loop ────────────────────────────────────


def _cursor_path(cursors_dir: Path, key: str) -> Path:
    return cursors_dir / f"{protocol.key_slug(key)}.json"


def _resolve_stamp(session: Session) -> tuple:
    """What a transcript path was resolved from; see ``_may_have_moved``.
    ``mirrored`` is in it so a restore (the flag clears, nothing else
    moves) re-resolves to the live file at once."""
    return (session.session_id, session.account_home, session.timestamp,
            bool(getattr(session, "mirrored", False)))


class Tailer:
    """One session's source plus its durable cursor."""

    def __init__(
        self,
        key: str,
        session: Session,
        source,
        cursors_dir: Path,
        has_journal: bool,
    ) -> None:
        self.key = key
        self.session = session
        self.source = source
        self._cursor_path = _cursor_path(cursors_dir, key)
        state = self._load_cursor()
        # A cursor the source can't verify as its own (another file
        # after a resume in another folder, a rewrite while down) counts
        # as none: resuming at 0 would re-journal the whole transcript.
        if not (state and source.restore(state)):
            state = {}
            if has_journal:
                # Journal exists but the cursor is gone: skip backfill
                # so history is not duplicated; live appends still land.
                source.skip_to_end()
            elif hasattr(source, "start_near_end"):
                # First sight of this session: recent history only.
                source.start_near_end()
        self.next_poll_at = 0.0
        # The session timestamp the transcript path was last resolved
        # at; see ``TailManager._may_have_moved``.
        self.resolved_stamp = _resolve_stamp(session)
        # A source waiting to replace ``source``; see ``hand_off``.
        self._handoff = None
        self._handoff_lock = threading.Lock()
        self._pending_save = False
        # (source, state) as the last poll left them; see ``poll``.
        self._polled: tuple | None = None
        # Whether the last poll moved the source (bytes, rows or
        # fingerprints read), with or without a message to show: a
        # turn-ending record that decodes to nothing still moves it.
        self.moved = False
        # What the cursor file holds now. A poll that leaves the source
        # here writes nothing, so the 0.25 s hot cadence and the 1 s
        # active one cost a read, not a file write, while nothing moves.
        self._saved: dict | None = state or None

    def hand_off(self, source) -> None:
        """Replace the source at the next ``poll``, on the tail thread
        (any thread may call this). A JSONL source continues after the
        last line the current one read when that line is in its file;
        otherwise it keeps the placement it came with."""
        with self._handoff_lock:
            self._handoff = source

    def next_source(self):
        """The source ``poll`` reads next: a pending hand-off, else the
        current one."""
        with self._handoff_lock:
            return self._handoff or self.source

    def _take_handoff(self) -> None:
        with self._handoff_lock:
            source, self._handoff = self._handoff, None
        if source is None:
            return
        old = self.source
        if isinstance(old, JsonlTailSource) and isinstance(
                source, JsonlTailSource):
            source.continue_from(old)
        self.source = source
        self._pending_save = True

    def _load_cursor(self) -> dict:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_cursor(self) -> None:
        state = self.source.state()
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, self._cursor_path)
        except OSError:
            return
        self._saved = state

    def due(self, now: float) -> bool:
        return now >= self.next_poll_at

    def interval(self, active: bool, hot: bool = False,
                 low_power: bool = False) -> float:
        """Seconds until the next poll: hot beats active beats idle.
        Low power stretches active and idle, never hot: a hot window
        only opens after a write, and a write means someone is there."""
        if low_power:
            base = POLL_ACTIVE_LOW_S if active else POLL_IDLE_LOW_S
        else:
            base = POLL_ACTIVE_S if active else POLL_IDLE_S
        if hot:
            return min(base, getattr(self.source, "hot_poll_s", POLL_HOT_S))
        return base

    def poll(self, now: float, active: bool, hot: bool = False,
             low_power: bool = False) -> list[dict]:
        """Poll the source; the cursor is only marked dirty here.

        ``commit_cursor`` runs the save — the manager calls it after the
        events are journaled, so a crash between poll and write replays
        (duplicates, recoverable) instead of skipping messages forever.
        """
        self.next_poll_at = now + self.interval(active, hot, low_power)
        self._take_handoff()
        # The state the last poll left is this poll's "before", unless
        # the source was replaced since (a resume moved the transcript):
        # one state() per poll, not two.
        last = self._polled
        before = last[1] if last and last[0] is self.source \
            else self.source.state()
        out = self.source.poll()
        after = self.source.state()
        self._polled = (self.source, after)
        self.moved = bool(out) or after != before
        # A source can move without emitting (records that decode to
        # nothing, an opencode row still streaming), so compare the
        # state rather than trusting ``out`` alone.
        if out or after != self._saved:
            self._pending_save = True
        return out

    def commit_cursor(self) -> None:
        if self._pending_save:
            self._pending_save = False
            self.save_cursor()


class TailManager:
    """Owns the tailers; ``emit`` is the one funnel events leave through.

    ``emit(session_key, event_dict)`` is supplied by the caller — the hub
    journals + broadcasts it, a satellite forwards it upstream — so this
    class never touches the network.
    """

    def __init__(
        self,
        emit: Callable[[str, dict], None],
        machine_id: str,
        home: Path | str | None = None,
        journal_has: Callable[[str], bool] | None = None,
        max_sessions: int = TAIL_MAX_SESSIONS,
        status_of: Callable[[list[Session]], list[str]] | None = None,
        on_status: Callable[[str, str, float], None] | None = None,
        read_clock: Callable[[], float] = time.time,
    ) -> None:
        self.emit = emit
        self.machine_id = machine_id
        self.home = Path(home) if home is not None else None
        self.cursors_dir = remote_dir_for(self.home) / "cursors"
        self._journal_has = journal_has or (lambda key: False)
        self.max_sessions = max_sessions
        # Written by the catalog thread (``sync``) and request threads
        # (``ensure``), read by the tail thread: every insert, removal
        # and iteration holds ``_status_lock`` (see ``_tailer_items``).
        self.tailers: dict[str, Tailer] = {}
        self.statuses: dict[str, str] = {}
        # Last time each key emitted a transcript append; the attach
        # idle check reads this server-side.
        self.last_append: dict[str, float] = {}
        # key -> (request_id, prompt fingerprint) of the approval the
        # pane watcher last issued; when its last pass ran.
        self._approval_outstanding: dict[str, tuple[str, str]] = {}
        self._approvals_at = float("-inf")
        # Wake state, written by request threads (``wake``) and read by
        # the tail thread (``tick``); nothing else here is shared that
        # way, so this lock guards only these three. ``_woken`` holds
        # keys owed an immediate poll; ``tick`` swaps it out and clears
        # the event in one step, so a wake landing mid-tick stays set
        # and the next ``wait`` returns at once instead of being lost.
        self._wake_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._woken: set[str] = set()
        self._hot_until: dict[str, float] = {}
        # Status probes between catalog refreshes. ``status_of`` is
        # ``session_statuses`` (None turns probing off); ``on_status``
        # tells the catalog so its rows match. ``statuses`` is written
        # by the catalog thread (``sync``) and the tail thread (probes),
        # so every write goes through ``_take_status`` under
        # ``_status_lock``, which also orders the events it emits.
        # ``_status_at`` is when each key's status was read; an older
        # read never replaces a newer one. ``_probe_due`` (keys whose
        # transcript grew since their last probe) and ``_probed_at``
        # belong to the tail thread alone.
        self._status_of = status_of
        self._on_status = on_status
        # Stamps a probe's read. The tick clock only schedules: a slow
        # tick would otherwise date a fresh read before a catalog
        # refresh that it actually followed.
        self._read_clock = read_clock
        self._status_lock = threading.RLock()
        self._status_at: dict[str, float] = {}
        self._probe_due: set[str] = set()
        self._probed_at: dict[str, float] = {}
        # key -> (identity, rang at) of the last ``limit`` notify, so a
        # retry into the same limit doesn't ring again. Tail thread only.
        self._limit_seen: dict[str, tuple] = {}
        # Orders tailer creation (which reads a key's cursor and asks
        # whether it has a journal) against ``sweep_stale`` deleting
        # them; see ``_add_tailer``.
        self._files_lock = threading.Lock()
        # Set by the hub while nobody watches; see ``set_low_power``.
        self.low_power = False

    def last_append_at(self, key: str) -> float | None:
        return self.last_append.get(key)

    # -- waking --------------------------------------------------------------

    def wake(self, key: str, now: float | None = None) -> None:
        """Something was just written to ``key``'s session: poll it now.

        Safe from any thread. Opens (or extends) the key's hot window
        and wakes a loop parked in ``wait``. A key with no tailer yet
        keeps its window, so an ``ensure`` that follows starts hot.
        """
        now = time.time() if now is None else now
        with self._wake_lock:
            until = now + HOT_WINDOW_S
            if until > self._hot_until.get(key, 0.0):
                self._hot_until[key] = until
            self._woken.add(key)
            self._wake_event.set()

    def interrupt(self) -> None:
        """End a ``wait`` early without scheduling a poll (shutdown)."""
        self._wake_event.set()

    def wait(self, timeout: float) -> bool:
        """Park the tail loop until ``timeout`` or the next wake."""
        return self._wake_event.wait(timeout)

    def is_hot(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._wake_lock:
            return self._hot_until.get(key, 0.0) > now

    def any_hot(self, now: float | None = None) -> bool:
        """Whether any session's hot window is open."""
        now = time.time() if now is None else now
        with self._wake_lock:
            return any(t > now for t in self._hot_until.values())

    def waking(self, key: str, injector):
        """``injector`` with a wake before and after each write it makes."""
        return _WakingInjector(injector, lambda: self.wake(key))

    def set_low_power(self, on: bool) -> None:
        """Poll at the low-power cadence (``POLL_*_LOW_S``) or not.

        Safe from any thread. Leaving low power marks every tailer owed
        a poll (as ``wake`` does, without the hot window) and wakes the
        loop: a session idle under low power may not be due for another
        minute, and the client that just arrived should not wait for it.
        """
        if on == self.low_power:
            return
        self.low_power = on
        if on:
            return
        keys = [key for key, _t in self._tailer_items()]
        with self._wake_lock:
            self._woken.update(keys)
            self._wake_event.set()

    def _take_wakes(self, now: float) -> tuple[set[str], dict[str, float]]:
        with self._wake_lock:
            woken, self._woken = self._woken, set()
            self._wake_event.clear()
            self._hot_until = {
                k: t for k, t in self._hot_until.items() if t > now
            }
            return woken, dict(self._hot_until)

    def ensure(self, key: str, session: Session, status: str) -> None:
        """Start a tailer on demand — a client opened this session.

        Background tailing stays capped at ``max_sessions``; a phone
        opening an older session gets history and live updates anyway.
        """
        if key in self.tailers:
            return
        source = make_source(session)
        if source is None:
            return
        if self._add_tailer(key, session, source):
            with self._status_lock:
                self.statuses[key] = status

    def _add_tailer(self, key: str, session: Session, source) -> bool:
        """Register a tailer for ``key`` unless another thread got there
        first; two tailers on one session would journal it twice."""
        # Under ``_files_lock``: a sweep can't delete this key's journal
        # and cursor between the tailer reading them and registering.
        with self._files_lock:
            tailer = Tailer(
                key, session, source, self.cursors_dir,
                self._journal_has(key),
            )
            with self._status_lock:
                if key in self.tailers:
                    return False
                self.tailers[key] = tailer
                return True

    def _tailer_items(self) -> list[tuple[str, "Tailer"]]:
        """A snapshot of the tailers, safe to walk while ``ensure``
        inserts from a request thread."""
        with self._status_lock:
            return list(self.tailers.items())

    def _source_path(self, source) -> Path | None:
        path = getattr(source, "path", None)
        return Path(path) if path else None

    def _may_have_moved(self, tailer: Tailer, session: Session) -> bool:
        """Whether ``_reresolve`` needs to look for a new transcript.

        Resolving globs every project folder (Claude) or walks the
        session tree, and ran for every tailer on every catalog
        refresh. A transcript can only have moved when the catalog saw
        a write since the last look, so this says yes only once the
        session's timestamp (or its id or account home) changed. For
        Claude the timestamp is the newest copy's mtime in ms: when it
        is the tailed file's own, nothing moved either.
        """
        stamp = _resolve_stamp(session)
        if stamp == tailer.resolved_stamp:
            return False
        path = self._source_path(tailer.source)
        same_file = stamp[:2] == tailer.resolved_stamp[:2]
        # A mirror copy keeps its source's mtime, so its stat would say
        # "nothing moved" right after a restore.
        if session.tool_name == "claude" and same_file and path is not None \
                and not getattr(tailer.source, "read_only", False):
            try:
                if path.stat().st_mtime * 1000 >= session.timestamp:
                    tailer.resolved_stamp = stamp
                    return False
            except OSError:
                pass
        tailer.resolved_stamp = stamp
        return True

    def _reresolve(self, tailer: Tailer, session: Session) -> None:
        """Follow a resumed session into its new transcript file.

        Claude ``--resume`` from another folder writes a new JSONL under
        that project; the old source would keep polling a dead path.
        The journal already holds the history the new file copies, so
        the fresh source continues after the last line the old one read
        (``continue_from``): turns written past the copy before this
        refresh still stream. That runs on the tail thread at its next
        poll (``Tailer.hand_off``), so the old source can't read on
        meanwhile. When the line isn't there, it starts at the end the
        file has now.
        """
        new_path = resolve_jsonl_path(session)
        if new_path is None or new_path == self._source_path(
                tailer.next_source()):
            return
        source = make_source(session)
        if source is None:
            return
        source.skip_to_end()
        tailer.hand_off(source)

    def sync(
        self,
        rows: list[tuple[str, Session, str]],
        as_of: float | None = None,
    ) -> None:
        """Reconcile tailers with the catalog.

        ``rows`` is ``(session_key, session, status)`` newest-first. New
        keys in the top slice get a source when resolvable; gone keys
        drop their tailer; status transitions emit ``session.status``
        and ``notify``. On-demand tailers (``ensure``) outside the top
        slice keep running while their session exists.

        ``as_of`` is when the catalog read these statuses. A key a probe
        read more recently keeps the probe's status: the catalog's is
        older news, and taking it would flip the phone back. None means
        untimed (tests): the catalog's status always applies.
        """
        by_key = {key: (s, st) for key, s, st in rows}
        wanted = rows[: self.max_sessions]
        wanted_keys = {key for key, _s, _st in wanted}
        for key, _tailer in self._tailer_items():
            if key not in by_key:
                # Under the lock: a probe applies only while its tailer
                # is still the registered one, checked under this lock.
                with self._status_lock:
                    self.tailers.pop(key, None)
                    self.statuses.pop(key, None)
                    self._status_at.pop(key, None)
                    # Under the lock too: the pane watcher raises a card
                    # only while the tailer is registered, checked here.
                    self._forget(key)
                self._probe_due.discard(key)
                self._probed_at.pop(key, None)
        for key, session, status in wanted:
            tailer = self.tailers.get(key)
            if tailer is None:
                source = make_source(session)
                if source is None:
                    continue
                self._add_tailer(key, session, source)
            else:
                # Refresh the session object: title/cwd can change as the
                # aggregator re-parses; the source may need re-resolving
                # when a resume moved the transcript file.
                tailer.session = session
                if self._may_have_moved(tailer, session):
                    self._reresolve(tailer, session)
            self._take_status(key, status, as_of, announce_first=True)
        # On-demand tailers past the cap: refresh session/status but
        # keep them alive while their session still exists.
        for key, tailer in self._tailer_items():
            if key in wanted_keys:
                continue
            session, status = by_key.get(key, (None, None))
            if session is None:
                continue
            tailer.session = session
            if self._may_have_moved(tailer, session):
                self._reresolve(tailer, session)
            self._take_status(key, status, as_of, announce_first=False)

    def sweep_stale(
        self,
        journal,
        keep: Iterable[str] = (),
        now: float | None = None,
    ) -> int:
        """Delete the journal and cursor files of sessions nothing has
        touched for ``STALE_FILE_AGE_S``; returns how many files went.

        A journal goes once no append and no cursor save has reached it
        for that long; a cursor goes once its journal is gone too, so a
        session never keeps one without the other (a cursor alone would
        resume past history the phone no longer has; a journal alone
        would skip backfill). Keys being tailed and ``keep`` (the
        caller's own journals, like the global stream) are never swept.
        A session that comes back is tailed like a new one: recent
        history only. Holds ``_files_lock``, so no tailer starts on a
        key's files while they are being judged and deleted.
        """
        with self._files_lock:
            return self._sweep_stale(journal, keep, now)

    def _sweep_stale(self, journal, keep: Iterable[str],
                     now: float | None) -> int:
        now = time.time() if now is None else now
        cutoff = now - STALE_FILE_AGE_S
        held = {protocol.key_slug(k) for k in keep}
        held.update(protocol.key_slug(k) for k, _t in self._tailer_items())

        def fresh(path: Path) -> bool:
            try:
                return path.stat().st_mtime >= cutoff
            except OSError:
                return False

        def kept(slug: str) -> bool:
            return slug in held or fresh(self.cursors_dir / f"{slug}.json")

        removed = journal.sweep(cutoff, kept)
        try:
            cursors = list(self.cursors_dir.glob("*.json"))
        except OSError:
            cursors = []
        for path in cursors:
            slug = path.stem
            if slug in held or fresh(path):
                continue
            if (journal.dir / f"{slug}.jsonl").exists():
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def _forget(self, key: str) -> None:
        """Drop what the pane watcher and the attach check kept for a
        session that left the catalog. A prompt still outstanding is
        retired with ``approval.resolved``, so a phone's card for it
        doesn't wait forever on a session that's gone."""
        self.last_append.pop(key, None)
        self._limit_seen.pop(key, None)
        outstanding = self._approval_outstanding.pop(key, None)
        if outstanding is not None:
            supervise.set_pending_approval(key, None)
            self._emit_approval(
                "approval.resolved", key,
                {"request_id": outstanding[0], "source": "gone"})

    def _take_status(
        self,
        key: str,
        status: str,
        at: float | None,
        announce_first: bool,
    ) -> bool:
        """Adopt ``status`` for ``key`` unless a newer read exists.

        Returns False only when a newer read won. Emits ``session.status``
        (and ``notify`` on a real transition) under the lock, so events
        leave in the order the statuses were taken.
        """
        with self._status_lock:
            if at is not None and self._status_at.get(key, float("-inf")) > at:
                return False
            if at is None:
                self._status_at.pop(key, None)
            else:
                self._status_at[key] = at
            previous = self.statuses.get(key)
            if previous == status:
                return True
            self.statuses[key] = status
            if previous is not None or (announce_first and status):
                self._emit_status(key, status)
            # A notify only fires on a real transition; the first
            # sighting of a session already in error/followup is
            # state, not news (and would spam push at hub startup).
            if previous is not None:
                self._emit_notify(key, previous, status)
            return True

    def _probe_session(self, key: str, tailer: "Tailer") -> Session:
        """The tailer's session with its last-activity time brought up
        to date. The catalog's copy can be minutes old for a session
        that was idle until just now, and the status probe calls a
        mid-turn transcript "interrupted" once that time is over
        ``ACTIVE_WINDOW_S`` old."""
        return self._freshen(key, tailer, tailer.session)

    def fresh_session(self, key: str, session: Session) -> Session:
        """``session`` (a catalog copy) with its last-activity time
        brought up to date from ``key``'s tailer, for a status probe
        made outside the tail loop (a queue-mode send's idle wait).
        Unchanged when nothing tails ``key``."""
        tailer = self.tailers.get(key)
        if tailer is None:
            return session
        return self._freshen(key, tailer, session)

    def _freshen(
        self, key: str, tailer: "Tailer", session: Session
    ) -> Session:
        ts = session.timestamp
        path = getattr(tailer.source, "path", None)
        if path:
            try:
                ts = max(ts, os.stat(path).st_mtime * 1000)
            except OSError:
                pass
        last = self.last_append.get(key)
        if last:
            ts = max(ts, last * 1000)
        if ts == session.timestamp:
            return session
        return dataclasses.replace(session, timestamp=ts)

    def _wants_probe(self, key: str, now: float, hot: bool) -> bool:
        """Whether ``key`` is owed a status probe this tick: hot or its
        source moved since the last one, at most every STATUS_PROBE_S."""
        if self._status_of is None:
            return False
        if not hot and key not in self._probe_due:
            return False
        if now - self._probed_at.get(key, float("-inf")) < STATUS_PROBE_S:
            return False
        self._probed_at[key] = now
        self._probe_due.discard(key)
        return True

    def _probe(self, due: list[tuple[str, "Tailer"]]) -> None:
        """One ``status_of`` call for every session owed a probe.

        ``session_statuses`` reads the live-pid registry, the bg-job
        table and the status tail cache once per call, so a batch costs
        about what one session does. A result applies only if its tailer
        is still the registered one: ``sync`` may drop (or replace) the
        session while the read runs, and a removed session must not
        come back as a status event.
        """
        if not due:
            return
        sessions = [self._probe_session(key, tailer) for key, tailer in due]
        read_at = self._read_clock()
        try:
            got = self._status_of(sessions)
        except Exception:
            return
        for (key, tailer), status in zip(due, got or []):
            status = str(status or "")
            with self._status_lock:
                if self.tailers.get(key) is not tailer:
                    continue
                # The catalog hears every accepted probe, changed or
                # not, so a refresh that read an older state cannot
                # leave its row disagreeing with the phone.
                if self._take_status(key, status, read_at,
                                     announce_first=True) \
                        and self._on_status is not None:
                    try:
                        self._on_status(key, status, read_at)
                    except Exception:
                        pass

    def _emit_status(self, key: str, status: str) -> None:
        self.emit(
            key,
            protocol.event(
                "session.status", key, self.machine_id, {"status": status}
            ),
        )

    def _note_limit(self, key: str, msg: dict, info: dict,
                    now: float) -> None:
        """Raise ``notify`` kind ``limit`` for a fresh limit record.

        Rings once per limit: a record read long after it was written
        (backfill) or the same limit again (a retry into it) is quiet.
        """
        ts = msg.get("ts")
        if isinstance(ts, (int, float)) and now - float(ts) > LIMIT_FRESH_S:
            return
        resets_at = info.get("resets_at")
        identity = (info.get("window"), resets_at) if resets_at \
            else msg.get("text")
        seen = self._limit_seen.get(key)
        if seen is not None and seen[0] == identity:
            quiet_until = resets_at if resets_at \
                else seen[1] + LIMIT_REPEAT_S
            if now < quiet_until:
                return
        self._limit_seen[key] = (identity, now)
        data: dict = {"kind": "limit",
                      "status": self.statuses.get(key, ""),
                      "message": str(msg.get("text") or "")[:280]}
        data.update(info)
        self.emit(key, protocol.event("notify", key, self.machine_id, data))

    def _emit_notify(self, key: str, old: str, new: str) -> None:
        # needs_you on a fresh followup, finished on active→done,
        # error whenever a session turns over to error.
        if new == "followup" and old != "followup":
            kind = "needs_you"
        elif new == "done" and old == "active":
            kind = "finished"
        elif new == "error" and old != "error":
            kind = "error"
        else:
            return
        self.emit(
            key,
            protocol.event(
                "notify", key, self.machine_id,
                {"kind": kind, "status": new},
            ),
        )

    def tick(self, now: float | None = None) -> int:
        """Poll every due tailer; returns events emitted."""
        now = time.time() if now is None else now
        woken, hot_until = self._take_wakes(now)
        n = 0
        probe_due: list[tuple[str, Tailer]] = []
        for key, tailer in self._tailer_items():
            if key not in woken and not tailer.due(now):
                # Not due for a read, but a source that moved since its
                # last probe still gets one once the gate opens.
                if self._wants_probe(key, now, key in hot_until):
                    probe_due.append((key, tailer))
                continue
            try:
                messages = tailer.poll(
                    now, active=self.statuses.get(key) == "active",
                    hot=key in hot_until, low_power=self.low_power,
                )
            except Exception:
                tailer.next_poll_at = now + POLL_IDLE_S
                continue
            for msg in messages:
                limit = msg.pop(LIMIT_MARK, None)
                self.emit(
                    key,
                    protocol.event(
                        "message.appended", key, self.machine_id, msg
                    ),
                )
                n += 1
                if limit is not None:
                    self._note_limit(key, msg, limit, now)
            if messages:
                self.last_append[key] = now
            if tailer.moved:
                self._probe_due.add(key)
            tailer.commit_cursor()
            if self._wants_probe(key, now, key in hot_until):
                probe_due.append((key, tailer))
        self._probe(probe_due)
        self._poll_approvals(now)
        return n

    def _poll_approvals(self, now: float) -> None:
        """Watch supervised tmux panes for permission prompts.

        The transcript never shows a tmux-mode approval menu, so a
        phone notification has to come from the pane itself. A prompt
        appearing emits ``approval.requested`` with a fresh request_id;
        it disappearing emits ``approval.resolved``. Cheap: one read of
        the supervised registry per pass, every PANE_APPROVAL_S, and one
        capture-pane per tmux-attached session. It read the registry once
        per tailer, up to a hundred reads a pass (#1530).
        """
        if now - self._approvals_at < PANE_APPROVAL_S:
            return
        self._approvals_at = now
        entries = supervise.registry(self.home)
        for key, _tailer in self._tailer_items():
            meta = entries.get(key)
            if not meta or meta.get("mode") != "tmux":
                if key in self._approval_outstanding:
                    rid, _fp = self._approval_outstanding.pop(key)
                    supervise.set_pending_approval(key, None)
                    self._emit_approval(
                        "approval.resolved", key,
                        {"request_id": rid, "source": "detached"},
                    )
                continue
            # The pane writes go to: the registry's, else the derived name
            # (``supervise.channel_name`` without reading the file again).
            pane = supervise.capture_pane(
                str(meta.get("tmux") or "") or supervise.derived_pane(key)
            )
            if key not in self.tailers:
                # sync dropped the session while its pane was read:
                # _forget already retired it; don't raise a new card.
                continue
            if supervise.pane_shows_approval(pane):
                fp = supervise.approval_fingerprint(pane)
                outstanding = self._approval_outstanding.get(key)
                if outstanding is not None and outstanding[1] != fp:
                    # Prompt A was dismissed and prompt B appeared
                    # between polls — retiring A's id so a delayed
                    # reply cannot answer B.
                    supervise.set_pending_approval(key, None)
                    self._emit_approval(
                        "approval.resolved", key,
                        {"request_id": outstanding[0],
                         "source": "superseded"},
                    )
                    outstanding = None
                if outstanding is None:
                    with self._status_lock:
                        if key not in self.tailers:
                            continue  # dropped since the check above
                        rid = f"req-{secrets.token_hex(6)}"
                        self._approval_outstanding[key] = (rid, fp)
                        supervise.set_pending_approval(key, rid, fp)
                        prompt = _approval_prompt_line(pane)
                        # The menu rows the pane offers, so a client can show
                        # and answer the real prompt instead of guessing at
                        # allow/always/deny. Numbered rows carry their own
                        # digit; an unnumbered arrow-select list carries
                        # positional ids the injector walks with arrows.
                        # Labels are pane text: redact.
                        options = [
                            {"n": n, "label": _tr._redact_secrets(label)[:140]}
                            for n, label in supervise.option_menu(pane)[0]
                        ]
                        self._emit_approval(
                            "approval.requested", key,
                            {"request_id": rid, "prompt": prompt,
                             "options": options},
                        )
            else:
                cur = self._approval_outstanding.pop(key, None)
                if cur is not None:
                    supervise.set_pending_approval(key, None)
                    self._emit_approval(
                        "approval.resolved", key,
                        {"request_id": cur[0], "source": "dismissed"},
                    )

    def _emit_approval(self, etype: str, key: str, data: dict) -> None:
        self.emit(
            key,
            protocol.event(etype, key, self.machine_id, data),
        )


class _WakingInjector:
    """Wraps an injector so its writes wake the session's tailer.

    The wake goes out before the write (the harness can react at once)
    and again after it returns: a headless claude ``send`` blocks for
    the whole turn, and the reply that follows deserves its own window.
    Everything else passes through untouched.
    """

    _WRITES = frozenset({"send", "approval", "control"})

    def __init__(self, inner, wake: Callable[[], None]) -> None:
        self._inner = inner
        self._wake = wake

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if name not in self._WRITES or not callable(attr):
            return attr

        def write(*args, **kwargs):
            self._wake()
            try:
                return attr(*args, **kwargs)
            finally:
                self._wake()

        return write
