"""Harness logs to ledger records.

Ports ``ClaudeLogParser.swift``, ``CodexFileParser.swift``, the OpenCode
and Devin readers in ``DatabaseSources.swift``, and the Pi, Droid, Cline
and Grok readers in ``SessionFileParsers.swift``. Keys and token splits
must stay identical to the Swift ones, so both ledgers count a response
once and the same way.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from swe.ledger.models import (
    LedgerRecord,
    UsageEvent,
    first_present,
    json_int,
    json_str,
    parse_timestamp,
)

# ---------------------------------------------------------------- Claude

_USAGE_MARKER = b'"usage"'
_SEMVER_PREFIX = re.compile(r"^\d+\.\d+\.\d")


def claude_records(line: bytes, account: str) -> list[LedgerRecord]:
    """Records from one Claude Code transcript line.

    Claude Code writes a line per content block of a response, all with
    the same ``message.id`` and ``requestId`` and a growing output count,
    and a resumed session copies earlier responses into its new file, so
    the key is the message id plus the request id; the store keeps the
    copy with the largest total.
    """
    if _USAGE_MARKER not in line:
        return []
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a line nested too deep to parse. Raised, it
        # stopped the scan at this line on every run, cursor unmoved.
        return []
    if not isinstance(obj, dict):
        return []
    message = obj.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return []
    # Swift's ``is NSNumber``, which a JSON bool also passes.
    if not all(isinstance(usage.get(k), (int, float))
               for k in ("input_tokens", "output_tokens")):
        return []
    ts_text = obj.get("timestamp")
    ts = parse_timestamp(ts_text)
    if ts is None or not _claude_valid(obj, message):
        return []

    message_id = json_str(message.get("id"))
    request_id = json_str(obj.get("requestId"))
    session = json_str(obj.get("sessionId")) or "unknown"
    cwd = json_str(obj.get("cwd"))
    sidechain = obj.get("isSidechain") is True
    model = json_str(message.get("model")) or "unknown"
    tier = "fast" if usage.get("speed") == "fast" else None
    cost = obj.get("costUSD")
    logged = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None

    if message_id:
        base = f"claude:{message_id}:{request_id}" if request_id else f"claude:{message_id}"
    else:
        # No id to collide on: key by position so a rescan stays idempotent.
        base = f"claude:anon:{session}:{ts_text}:{model}"

    def event(source: dict, model_name: str, logged_cost) -> UsageEvent:
        ev = UsageEvent(harness="claude", account=account, session=session, cwd=cwd,
                        model=model_name, ts=ts, cost=logged_cost, tier=tier)
        _claude_tokens(source, ev)
        return ev

    out: list[LedgerRecord] = []
    ev = event(usage, model, logged)
    if ev.total > 0 or (logged or 0) > 0:
        out.append(LedgerRecord(key=base, event=ev, msg=message_id, sidechain=sidechain))

    # Advisor calls run a second model inside one response and are not in
    # the parent's usage; ordinary iterations are, so they are skipped.
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        index = 0
        for it in iterations:
            if not isinstance(it, dict) or it.get("type") != "advisor_message":
                continue
            advisor = json_str(it.get("model"))
            if advisor is None:
                continue
            ev = event(it, advisor, None)
            if ev.total > 0:
                out.append(LedgerRecord(
                    key=f"{base}:advisor:{index}", event=ev,
                    msg=f"{message_id}:advisor:{index}" if message_id else None,
                    sidechain=sidechain,
                ))
            index += 1
    return out


def _claude_tokens(usage: dict, ev: UsageEvent) -> None:
    output = json_int(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    thinking = None
    if isinstance(details, dict):
        thinking = first_present(details, "thinking_tokens", "reasoning_tokens")
    reasoning = min(output, json_int(thinking))
    ev.input = json_int(usage.get("input_tokens"))
    ev.output = output - reasoning
    ev.reasoning = reasoning
    ev.cache_read = json_int(usage.get("cache_read_input_tokens"))
    aggregate = json_int(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        five = json_int(split.get("ephemeral_5m_input_tokens"))
        hour = json_int(split.get("ephemeral_1h_input_tokens"))
        ev.cache_write = max(aggregate, five + hour)
        ev.cache_write_1h = hour
    else:
        ev.cache_write = aggregate


def _claude_valid(obj: dict, message: dict) -> bool:
    """ccusage's rules: a non-semver ``version`` marks a foreign format, and
    ids or a model that are present but empty mark a malformed line."""
    version = obj.get("version")
    if isinstance(version, str) and not _SEMVER_PREFIX.match(version):
        return False
    for value in (obj.get("sessionId"), obj.get("requestId"),
                  message.get("id"), message.get("model")):
        if isinstance(value, str) and value == "":
            return False
    return True


# ---------------------------------------------------------------- Codex

_CODEX_MARKERS = (b'"session_meta"', b'"turn_context"', b'"token_count"',
                  b'"token_usage_record"', b'"task_started"',
                  b'"thread_settings_applied"')
CODEX_FALLBACK_MODEL = "gpt-5"


def _codex_usage(obj: dict) -> tuple[int, int, int, int, int, int]:
    """(input, cached, cache_write, output, reasoning, total)."""
    return (
        json_int(first_present(obj, "input_tokens", "prompt_tokens")),
        json_int(first_present(obj, "cached_input_tokens", "cache_read_input_tokens")),
        json_int(first_present(obj, "cache_write_input_tokens", "cache_creation_input_tokens")),
        json_int(first_present(obj, "output_tokens", "completion_tokens")),
        json_int(first_present(obj, "reasoning_output_tokens", "reasoning_tokens")),
        json_int(obj.get("total_tokens")),
    )


def _floor(x: float) -> float:
    return float(int(x // 1))


class CodexFileParser:
    """One Codex rollout, read from its first line.

    Codex logs cumulative totals (``token_count``) or one record per
    response (``token_usage_record``). A resumed or compacted rollout
    replays old totals, so every total seen is remembered and a repeat
    emits nothing. A child thread (fork, sub-agent) replays its parent's
    history before its own first turn; nothing is emitted until its own
    ``task_started``.
    """

    def __init__(self, path: Path, account: str):
        self.account = account
        self.session = path.stem
        self.cwd: str | None = None
        self.model: str | None = None
        self.tier: str | None = None
        self.saw_meta = False
        self.gate: float | None = None
        self.seen: set[tuple] = set()
        self.last_total: tuple | None = None
        self.uses_records = False

    @staticmethod
    def wants(line: bytes) -> bool:
        return any(m in line for m in _CODEX_MARKERS)

    def records(self, obj: dict) -> list[LedgerRecord]:
        kind = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        ts_text = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        line_time = parse_timestamp(ts_text)

        if kind == "session_meta":
            if self.saw_meta:
                return []
            self.saw_meta = True
            self.cwd = json_str(payload.get("cwd")) or self.cwd
            if self._is_child(payload):
                created = parse_timestamp(payload.get("timestamp"))
                if created is None:
                    created = line_time
                self.gate = _floor(created) if created is not None else -1.0
            return []
        if kind == "turn_context":
            model = json_str(payload.get("model"))
            if model:
                self.model = model
            cwd = json_str(payload.get("cwd"))
            if cwd:
                self.cwd = cwd
            return []
        if kind == "token_usage_record":
            # Flip the flag only once a record actually counts: a line
            # missing `usage` or a timestamp must not silence the older
            # `token_count` lines after it — the flag persists in the
            # cursor, so a malformed record would undercount for good.
            usage = payload.get("usage")
            if not isinstance(usage, dict) or line_time is None:
                return []
            self.uses_records = True
            response_id = json_str(payload.get("response_id"))
            key = (f"codex:resp:{response_id}" if response_id
                   else f"codex:{self.session}:{ts_text or ''}:record")
            return self._record(_codex_usage(usage), key, line_time)
        if kind != "event_msg":
            return []

        sub = payload.get("type")
        if sub == "thread_settings_applied":
            settings = payload.get("thread_settings")
            raw = settings.get("service_tier") if isinstance(settings, dict) and "service_tier" in settings \
                else payload.get("service_tier")
            tier = json_str(raw)
            if tier:
                self.tier = normalized_tier(tier)
            return []
        if sub == "task_started":
            started = payload.get("started_at")
            if self.gate is not None and isinstance(started, (int, float)) and not isinstance(started, bool):
                if self.gate >= 0:
                    threshold = self.gate
                else:
                    threshold = _floor(line_time) if line_time is not None else float("inf")
                if started >= threshold:
                    self.gate = None
            return []
        if sub == "token_count":
            return self._token_count(payload, ts_text, line_time)
        return []

    def _token_count(self, payload: dict, ts_text, time) -> list[LedgerRecord]:
        info = payload.get("info")
        if not isinstance(info, dict) or time is None or ts_text is None:
            return []
        total_raw = info.get("total_token_usage")
        last_raw = info.get("last_token_usage")
        total = _codex_usage(total_raw) if isinstance(total_raw, dict) else None
        last = _codex_usage(last_raw) if isinstance(last_raw, dict) else None
        if self.gate is not None or self.uses_records:
            if total is not None:
                self._remember(total)
            return []
        if total is not None:
            if total in self.seen:
                return []
            if last is not None:
                usage = last
            else:
                prev = self.last_total or (0,) * 6
                usage = tuple(max(0, a - b) for a, b in zip(total, prev))
            self._remember(total)
        elif last is not None:
            usage = last
        else:
            return []
        return self._record(usage, f"codex:{self.session}:{ts_text}", time)

    def _remember(self, total: tuple) -> None:
        self.seen.add(total)
        self.last_total = total

    def _record(self, usage: tuple, key: str, time: float) -> list[LedgerRecord]:
        inp, cached, write, out, reas, _total = usage
        if not (inp or cached or out or reas or write):
            return []
        cached = min(cached, inp)
        write = min(write, inp - cached)
        reas = min(reas, out)
        ev = UsageEvent(
            harness="codex", account=self.account, session=self.session, cwd=self.cwd,
            model=self.model or CODEX_FALLBACK_MODEL, ts=time,
            input=inp - cached - write, output=out - reas,
            cache_read=cached, cache_write=write, reasoning=reas, tier=self.tier,
        )
        return [LedgerRecord(key=key, event=ev)]

    @staticmethod
    def _is_child(payload: dict) -> bool:
        if json_str(payload.get("forked_from_id")) or json_str(payload.get("parent_thread_id")):
            return True
        if payload.get("thread_source") == "subagent":
            return True
        source = payload.get("source")
        return isinstance(source, dict) and source.get("subagent") is not None


def normalized_tier(tier: str) -> str | None:
    t = tier.lower()
    if t in {"default", "auto", "standard"}:
        return None
    if t in {"fast", "priority"}:
        return "priority"
    return t


def codex_file_records(path: Path, account: str) -> list[LedgerRecord]:
    parser = CodexFileParser(path, account)
    out: list[LedgerRecord] = []
    for line in iter_lines(path):
        if not parser.wants(line):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            out.extend(parser.records(obj))
    return out


# ---------------------------------------------------------------- files

MAX_LINE = 64 << 20


def iter_lines(path: Path, offset: int = 0, end: list | None = None):
    """Complete, newline-terminated lines from ``offset``. A trailing
    partial line is left for the next read; ``end[0]`` is set to the
    offset just past the last complete line. Raises ``OSError`` when the
    file can't be opened."""
    pos = offset
    # An unreadable file raises, so the caller notes it and keeps its
    # cursor; a silent empty read would mark it done for good.
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break
            pos += len(line)
            if len(line) > MAX_LINE:
                continue
            yield line
    if end is not None:
        end.append(pos)


# ---------------------------------------------------------------- sqlite sources

def _open_ro(path: Path) -> sqlite3.Connection:
    # as_uri() percent-encodes the path: a raw "#", "?" or "%" in a home
    # folder's name would end the file: URI early or decode into another
    # path, as the remote readers' openers already guard against.
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro",
                           uri=True, timeout=5)


def _db_tokens(value) -> int:
    """A token count a database column holds, between 0 and
    ``MAX_TOKENS`` (``Statement.tokens`` in ``DatabaseSources.swift``),
    the cap the log readers' ``json_int`` applies. A corrupt count past
    SQLite's 64-bit range failed the insert, and one near it overflowed
    every later SUM over the ledger. A JSON string column reads as its
    number, else 0, as SQLite's own integer conversion does."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return 0
    return json_int(value)


def _row_int(value) -> int:
    """A watermark column as SQLite's integer read gives it (NULL and
    text that is not a number read as 0), for the readers' ``highest``."""
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def opencode_records(db: Path) -> list[LedgerRecord]:
    """Every OpenCode reply in ``db`` (``opencode_scan`` from the start)."""
    return opencode_scan(db, 0)[0]


def opencode_scan(db: Path, since: int = 0) -> tuple[list[LedgerRecord], int]:
    """``opencode.db`` assistant messages updated at or after ``since``
    (milliseconds), legacy ``message`` rows and 2.x ``session_message``
    ones, the model recorded as ``provider/model`` (see
    ``opencode_model``), with the highest ``time_updated`` read: the next
    scan's watermark (``scanOpenCode`` in ``DatabaseSources.swift``).
    Messages are rewritten while they stream, so the watermark is
    ``time_updated``, compared inclusively, and every row is keyed by its
    message id, so a re-read replaces in place. Older rows folded
    reasoning into output; a recorded cost of 0 means OpenCode did not
    know the price, so it is no recorded cost."""
    conn = _open_ro(db)
    try:
        rows = conn.execute("""
            SELECT m.id, m.session_id, m.time_created, m.time_updated, s.directory,
                json_extract(m.data, '$.modelID'), json_extract(m.data, '$.cost'),
                json_extract(m.data, '$.tokens.input'), json_extract(m.data, '$.tokens.output'),
                json_extract(m.data, '$.tokens.reasoning'), json_extract(m.data, '$.tokens.cache.read'),
                json_extract(m.data, '$.tokens.cache.write'), json_extract(m.data, '$.tokens.total'),
                json_extract(m.data, '$.providerID')
            FROM message m LEFT JOIN session s ON s.id = m.session_id
            WHERE m.time_updated >= ? AND json_extract(m.data, '$.role') = 'assistant'
            ORDER BY m.time_updated
        """, (since,)).fetchall()
        rows += _opencode_v2_rows(conn, since)
    finally:
        conn.close()
    account = str(db.parent)
    highest = since
    out: list[LedgerRecord] = []
    for (mid, session, created, updated, directory, model, cost,
         inp, outp, reas, cread, cwrite, total, provider) in rows:
        highest = max(highest, _row_int(updated))
        inp, outp, reas = _db_tokens(inp), _db_tokens(outp), _db_tokens(reas)
        cread, cwrite = _db_tokens(cread), _db_tokens(cwrite)
        inside = (reas > 0 and total is not None
                  and _db_tokens(total) == inp + outp + cread + cwrite)
        ev = UsageEvent(
            harness="opencode", account=account, session=session or "unknown",
            cwd=directory, model=opencode_model(provider, model), ts=(created or 0) / 1000,
            input=inp, output=max(0, outp - reas if inside else outp),
            cache_read=cread, cache_write=cwrite, reasoning=reas,
            cost=float(cost) if isinstance(cost, (int, float)) and cost > 0 else None,
        )
        if ev.total > 0:
            out.append(LedgerRecord(key=f"opencode:{mid or ''}", event=ev, relabels_model=True))
    return out, highest


def opencode_model(provider: str | None, model_id: str | None) -> str:
    """The ledger's model name for an OpenCode reply: ``provider/model``
    (OpenCode's own ``-m`` form, so the first slash always splits them; an
    id with a slash of its own, like OpenRouter's ``nvidia/nemotron-...``,
    keeps it), the bare id when no provider is recorded. Pricing strips
    the prefix again. Keep in step with ``openCodeModel`` in
    ``DatabaseSources.swift``."""
    if not model_id:
        return "unknown"
    if not isinstance(provider, str) or not provider:
        return model_id
    return f"{provider}/{model_id}"


def _opencode_v2_rows(conn: sqlite3.Connection, since: int = 0) -> list[tuple]:
    """OpenCode 2.x replies updated at or after ``since``, in
    ``opencode_scan``'s row shape.

    2.x writes new replies only to ``session_message`` (and new sessions
    only to ``session_v2``), naming the model ``model.id``. The 2.x
    migration copied old replies there under their ``message`` ids, so
    those are left to the ``message`` read. No ``tokens.total`` is kept:
    the counts are disjoint, as on current ``message`` rows.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "session_message" not in tables:
        return []
    directory = "s.directory"
    join = "LEFT JOIN session s ON s.id = sm.session_id"
    if "session_v2" in tables:
        directory = "COALESCE(v.directory, s.directory)"
        join += " LEFT JOIN session_v2 v ON v.id = sm.session_id"
    return conn.execute(f"""
        SELECT sm.id, sm.session_id, sm.time_created, sm.time_updated, {directory},
            json_extract(sm.data, '$.model.id'), json_extract(sm.data, '$.cost'),
            json_extract(sm.data, '$.tokens.input'), json_extract(sm.data, '$.tokens.output'),
            json_extract(sm.data, '$.tokens.reasoning'), json_extract(sm.data, '$.tokens.cache.read'),
            json_extract(sm.data, '$.tokens.cache.write'), NULL,
            json_extract(sm.data, '$.model.providerID')
        FROM session_message sm {join}
        WHERE sm.time_updated >= ? AND sm.type = 'assistant'
          AND NOT EXISTS (SELECT 1 FROM message m WHERE m.id = sm.id)
        ORDER BY sm.time_updated
    """, (since,)).fetchall()


def devin_records(db: Path) -> list[LedgerRecord]:
    """Every Devin reply in ``db`` (``devin_scan`` from the start)."""
    return devin_scan(db, 0)[0]


def devin_scan(db: Path, since: int = 0) -> tuple[list[LedgerRecord], int]:
    """Devin CLI ``sessions.db``: usage on assistant ``message_nodes``
    past the ``row_id`` watermark ``since``, with the next scan's
    watermark (``scanDevin`` in ``DatabaseSources.swift``). Replays copy
    nodes, so ``request_id`` is the key across the install.

    Row ids only grow (``AUTOINCREMENT``), so a highest id below
    ``since`` means the file was rebuilt in place, and it is read from
    the start. An assistant row still waiting for its metrics holds the
    watermark back so it is read again once Devin fills them in, unless a
    later row of the same session has metrics, which means it was
    abandoned."""
    conn = _open_ro(db)
    try:
        top = conn.execute("SELECT COALESCE(MAX(row_id), 0) FROM message_nodes").fetchone()
        if top is not None and _row_int(top[0]) < since:
            since = 0
        has_sessions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone() is not None
        rows = conn.execute(f"""
            SELECT n.row_id, n.session_id, n.created_at, {"s.working_directory" if has_sessions else "NULL"},
                json_extract(n.chat_message, '$.metadata.request_id'),
                json_extract(n.chat_message, '$.metadata.generation_model'),
                json_extract(n.chat_message, '$.metadata.created_at'),
                json_extract(n.chat_message, '$.metadata.metrics.input_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.output_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.cache_read_tokens'),
                json_extract(n.chat_message, '$.metadata.metrics.cache_creation_tokens')
            FROM message_nodes n {"LEFT JOIN sessions s ON s.id = n.session_id" if has_sessions else ""}
            WHERE n.row_id > ? AND json_valid(n.chat_message)
                AND json_extract(n.chat_message, '$.role') = 'assistant'
                AND json_extract(n.chat_message, '$.metadata.request_id') IS NOT NULL
            ORDER BY n.row_id
        """, (since,)).fetchall()
    finally:
        conn.close()
    account = str(db.parent)
    highest = since
    # Session to its earliest row still waiting for metrics.
    pending: dict[str, int] = {}
    out: list[LedgerRecord] = []
    for (row_id, session, created, cwd, request_id, model, meta_created,
         inp, outp, cread, cwrite) in rows:
        row_id = _row_int(row_id)
        session = session or "unknown"
        highest = max(highest, row_id)
        if inp is None or outp is None:
            pending.setdefault(session, row_id)  # still waiting for metrics
            continue
        pending.pop(session, None)
        ts = parse_timestamp(meta_created)
        if ts is None:
            ts = float(created or 0)
        ev = UsageEvent(
            harness="devin", account=account, session=session, cwd=cwd,
            model=model or "unknown", ts=ts,
            input=_db_tokens(inp), output=_db_tokens(outp),
            cache_read=_db_tokens(cread), cache_write=_db_tokens(cwrite),
        )
        if ev.total > 0 and request_id:
            out.append(LedgerRecord(key=f"devin:{request_id}", event=ev))
    return out, (min(pending.values()) - 1 if pending else highest)


# ---------------------------------------------------------------- Pi

_PI_MARKERS = (b'"message"', b'"session"')


class PiFileParser:
    """One Pi session log (``<pi home>/agent/sessions/``), read line by
    line (``PiFileParser`` in ``SessionFileParsers.swift``).

    Assistant ``message`` lines carry a ``usage`` block with per-request
    counts and a USD ``cost`` Pi computed itself. ``usage.output`` already
    includes ``reasoning``, so reasoning is split back out of output the
    way the Codex reader splits it. The session's cwd arrives once on its
    ``session`` line, so it is kept as line-parser state. Timestamps are
    milliseconds.
    """

    def __init__(self, path: Path, account: str, cwd: str | None = None):
        self.path, self.account, self.cwd = path, account, cwd

    @staticmethod
    def wants(line: bytes) -> bool:
        return any(m in line for m in _PI_MARKERS)

    def records(self, obj: dict) -> list[LedgerRecord]:
        kind = obj.get("type")
        if kind == "session":
            cwd = json_str(obj.get("cwd"))
            if cwd:
                self.cwd = cwd
            return []
        if kind != "message":
            return []
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []
        usage = message.get("usage")
        ms = message.get("timestamp")
        # Swift's ``as? NSNumber``, which a JSON bool also passes.
        if not isinstance(usage, dict) or not isinstance(ms, (int, float)):
            return []
        output = json_int(usage.get("output"))
        reasoning = min(json_int(usage.get("reasoning")), output)
        cost = usage.get("cost")
        total_cost = cost.get("total") if isinstance(cost, dict) else None
        logged = (float(total_cost)
                  if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool)
                  else None)
        ev = UsageEvent(
            harness="pi", account=self.account, session=str(self.path), cwd=self.cwd,
            model=json_str(message.get("model")) or "unknown", ts=float(ms) / 1000,
            input=json_int(usage.get("input")), output=output - reasoning,
            cache_read=json_int(usage.get("cacheRead")),
            cache_write=json_int(usage.get("cacheWrite")), reasoning=reasoning,
            cost=logged)
        if not (ev.total > 0 or (logged or 0) > 0):
            return []
        identity = json_str(obj.get("id")) or f"{float(ms):.3f}"
        return [LedgerRecord(key=f"pi:{self.path.name}:{identity}", event=ev)]


def pi_file_records(path: Path, account: str) -> list[LedgerRecord]:
    parser = PiFileParser(path, account)
    out: list[LedgerRecord] = []
    for line in iter_lines(path):
        if not parser.wants(line):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            out.extend(parser.records(obj))
    return out


# ---------------------------------------------------------------- session totals


class Empty:
    """``SessionTotals.empty``: the session's file parses but reports no
    usage, so a previous scan's event under ``key`` is deleted, not left
    behind."""

    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key


def _read_json_object(path: Path) -> dict:
    """The file's JSON object (``SessionTotalsFile.readJSON``). ``OSError``
    and ``ValueError`` propagate, so the scan notes the file and keeps it
    unread — its cursor is saved only on a clean parse."""
    obj = json.loads(path.read_bytes())
    if not isinstance(obj, dict):
        raise ValueError(f"{path}: not a JSON object")
    return obj


def droid_transcript(settings: Path) -> Path:
    """The transcript a ``<uuid>.settings.json`` is derived from —
    ``<uuid>.jsonl`` beside it. Its stamp is part of the settings file's
    read cursor, so a transcript change alone re-reads the pair."""
    return settings.with_suffix("").with_suffix("").with_suffix(".jsonl")


def droid_session_totals(settings: Path, account: str):
    """Droid ``<droid home>/sessions/<project>/<uuid>.settings.json``
    (``SessionTotalsFile.droid``).

    The settings file mirrors the session's cumulative token usage; the
    transcript beside it supplies cwd and the timestamp of the last event
    (its own mtime when there is none — the whole-file guard is the scan's,
    like Swift's ``max(transcript?... , settings.mtime)``).
    """
    session_id = settings.with_suffix("").with_suffix("").name
    key = f"droid:{session_id}"
    obj = _read_json_object(settings)
    usage = first_present(obj, "inclusiveTokenUsage", "tokenUsage")
    if not isinstance(usage, dict):
        return Empty(key)
    input_ = json_int(usage.get("inputTokens"))
    output = json_int(usage.get("outputTokens"))
    cache_read = json_int(usage.get("cacheReadTokens"))
    cache_write = json_int(usage.get("cacheCreationTokens"))
    reasoning = json_int(usage.get("thinkingTokens"))
    if input_ + output + cache_read + cache_write + reasoning <= 0:
        return Empty(key)

    cwd = None
    last = settings.stat().st_mtime
    try:
        lines = open(droid_transcript(settings), "rb")
    except OSError:
        lines = None
    if lines is not None:
        with lines:
            try:
                for line in lines:
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("type") == "session_start" and cwd is None:
                        cwd = json_str(entry.get("cwd"))
                    stamp = parse_timestamp(entry.get("timestamp"))
                    if stamp is not None:
                        last = stamp
            except OSError:
                # A mid-read failure ends the scan of the transcript, as
                # a ``nil`` read does the line loop.
                pass

    ev = UsageEvent(
        harness="droid", account=account, session=session_id, cwd=cwd,
        model=json_str(obj.get("model")) or "unknown", ts=last,
        input=input_, output=output, cache_read=cache_read,
        cache_write=cache_write, reasoning=reasoning, session_total=True)
    return LedgerRecord(key=key, event=ev, authoritative=True)


def cline_session_totals(path: Path, account: str):
    """Cline ``<cline home>/data/sessions/<id>/<id>.json``
    (``SessionTotalsFile.cline``). ``None`` when the file is not a
    session's — a legacy shape the scan leaves alone rather than deletes."""
    obj = _read_json_object(path)
    session_id = json_str(obj.get("session_id"))
    if session_id is None:
        return None
    key = f"cline:{session_id}"
    metadata = obj.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    input_ = json_int(metadata.get("tokensIn"))
    output = json_int(metadata.get("tokensOut"))
    cache_read = json_int(metadata.get("cacheReads"))
    cache_write = json_int(metadata.get("cacheWrites"))
    logged = metadata.get("totalCost")
    cost = (float(logged)
            if isinstance(logged, (int, float)) and not isinstance(logged, bool)
            and logged > 0 else None)
    if input_ + output + cache_read + cache_write <= 0 and cost is None:
        return Empty(key)
    ts = parse_timestamp(obj.get("ended_at")) or parse_timestamp(obj.get("started_at"))
    if ts is None:
        return Empty(key)
    ev = UsageEvent(
        harness="cline", account=account, session=session_id,
        cwd=json_str(first_present(obj, "cwd", "workspace_root")),
        model=json_str(obj.get("model")) or "unknown", ts=ts,
        input=input_, output=output, cache_read=cache_read,
        cache_write=cache_write, cost=cost, session_total=True)
    return LedgerRecord(key=key, event=ev, authoritative=True)


# ---------------------------------------------------------------- Grok


# Grok's ``costUsdTicks`` is USD times this (a session of a few dollars
# lands in the hundreds of billions of ticks).
_GROK_TICKS_PER_USD = 10_000_000_000


def grok_session_turns(path: Path, account: str) -> list:
    """Grok ``<home>/sessions/<project>/<id>/usage.json``.

    One record per finished turn: the file rewrites cumulative totals in
    ``session`` and the same numbers split by turn, so the session block
    is never stored or it would double-count. ``costUsdTicks`` is Grok's
    own dollar figure and wins over the catalog. The list ends with one
    ``Empty`` per turn this file used to store and no longer does, so a
    dropped turn deletes its event and nothing else's.
    """
    session_id = path.parent.name
    prefix = f"grok:{session_id}:"
    obj = _read_json_object(path)
    turns = obj.get("turns")
    turns = turns if isinstance(turns, list) else []
    cwd = _grok_cwd(path)
    out: list = []
    seen: set[int] = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        number = turn.get("turnNumber")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            continue
        if number in seen:
            continue
        seen.add(number)
        key = f"{prefix}{number}"
        ts = parse_timestamp(turn.get("endedAt"))
        if ts is None:
            out.append(Empty(key))
            continue
        input_ = json_int(turn.get("inputTokens"))
        output = json_int(turn.get("outputTokens"))
        cache_read = json_int(turn.get("cachedReadTokens"))
        cache_write = json_int(turn.get("cacheCreationTokens"))
        reasoning = json_int(turn.get("reasoningTokens"))
        cost = _grok_cost(turn.get("costUsdTicks"))
        if input_ + output + cache_read + cache_write + reasoning <= 0 and cost is None:
            out.append(Empty(key))
            continue
        model = json_str(turn.get("primaryModelId")) or "unknown"
        ev = UsageEvent(
            harness="grok", account=account, session=session_id, cwd=cwd,
            model=model, ts=ts, input=input_, output=output,
            cache_read=cache_read, cache_write=cache_write, reasoning=reasoning,
            cost=cost)
        out.append(LedgerRecord(key=key, event=ev, authoritative=True,
                                skips_lookalikes=True))
    out.append(GrokDropped(prefix, seen))
    return out


class GrokDropped:
    """Turns this ``usage.json`` no longer lists. The scan deletes stored
    events under ``prefix`` whose turn number is absent from ``kept``."""

    __slots__ = ("prefix", "kept")

    def __init__(self, prefix: str, kept: set[int]):
        self.prefix = prefix
        self.kept = kept


def _grok_cwd(usage: Path) -> str | None:
    """The session's working directory, from ``prompt_context.json`` beside
    ``usage.json``. A missing or unreadable sibling leaves cwd unset; it
    is not part of the cursor, so a context written later waits for the
    next usage rewrite."""
    try:
        ctx = json.loads((usage.parent / "prompt_context.json").read_bytes())
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(ctx, dict):
        return None
    return json_str(ctx.get("working_directory"))


def _grok_cost(ticks) -> float | None:
    """USD from ``costUsdTicks``, or ``None`` so the catalog prices the
    turn. Zero and anything that is not a finite non-negative number
    fall through; a bool is a number in Python and is rejected."""
    if isinstance(ticks, bool) or not isinstance(ticks, (int, float)):
        return None
    if ticks != ticks or ticks <= 0 or ticks == float("inf"):
        return None
    return float(ticks) / _GROK_TICKS_PER_USD
