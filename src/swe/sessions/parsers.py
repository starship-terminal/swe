"""Per-harness session parsers (thin adapters over family engines)."""

from __future__ import annotations

import glob
import json
import os
import re
from urllib.parse import unquote

from swe.paths import account_homes_for
from swe.sessions.engines import (
    JsonParserConfig,
    JsonlParserConfig,
    SqliteParserConfig,
    clean_title,
    extract_user_text,
    get_mtime,
    parse_iso_ts,
    parse_json_store,
    parse_jsonl_projects,
    parse_sqlite,
    path_from_encoded_dir,
    strip_file_uri,
)
from swe.sessions.engines.jsonl_engine import event_cwd
from swe.sessions import cursor_store, failures, opencode_store, parse_cache, transcript_mirror
from swe.sessions.models import Session


# ---------------------------------------------------------------------------
# SQLite family
# ---------------------------------------------------------------------------

def _opencode_family_query(conn) -> str:
    """Session rows from ``session`` and opencode 2.x's ``session_v2``, once each.

    See ``opencode_store.sessions_query``: 2.x writes new sessions only to
    ``session_v2``, so reading ``session`` alone loses every session made
    since the upgrade.
    """
    return opencode_store.sessions_query(conn)


def parse_opencode():
    return parse_sqlite(
        SqliteParserConfig(
            tool_name="opencode",
            agent="OpenCode",
            db_path=os.path.expanduser("~/.local/share/opencode/opencode.db"),
            query=_opencode_family_query,
            updated=0,
            title=1,
            path=2,
            session_id=3,
            require_path=True,
        )
    )


def parse_kilo():
    # Kilo is an opencode fork: kilo.db keeps the same drizzle schema
    # (session/workspace/message/part, epoch-ms time_*, JSON data blobs).
    return parse_sqlite(
        SqliteParserConfig(
            tool_name="kilo",
            agent="Kilo",
            db_path=os.path.expanduser("~/.local/share/kilo/kilo.db"),
            query=_opencode_family_query,
            updated=0,
            title=1,
            path=2,
            session_id=3,
            require_path=True,
        )
    )


_COPILOT_USER_NAMED_RE = re.compile(r"^user_named:\s*(true|false)\s*$", re.IGNORECASE)
_COPILOT_NAME_RE = re.compile(r"^name:\s*(.*)$")
_COPILOT_BLOCK_SCALAR_RE = re.compile(r"^[|>][-+]?\d*$")
_COPILOT_YAML_MAX_LINES = 512


def _copilot_workspace_meta(state_root: str, sid: str) -> tuple[bool, str]:
    """Read ``user_named`` and ``name`` from a Copilot session's workspace.yaml.

    Stdlib only: scans top-level lines for the two keys and stops as soon as
    both are found. Missing file, missing key, or unreadable content all mean
    ``(False, "")``. A ``name: |-`` block scalar is folded onto one line.
    """
    if not sid or not state_root:
        return False, ""
    path = os.path.join(state_root, sid, "workspace.yaml")
    user_named: bool | None = None
    name: str | None = None
    block: list[str] | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh):
                if lineno >= _COPILOT_YAML_MAX_LINES:
                    break
                if block is not None:
                    if not line.strip() or line[0] in " \t":
                        block.append(line.strip())
                        continue
                    name = " ".join(part for part in block if part)
                    block = None
                if user_named is None:
                    m = _COPILOT_USER_NAMED_RE.match(line)
                    if m:
                        user_named = m.group(1).lower() == "true"
                        if name is not None:
                            break
                        continue
                if name is None:
                    m = _COPILOT_NAME_RE.match(line)
                    if m:
                        value = m.group(1).strip()
                        if _COPILOT_BLOCK_SCALAR_RE.match(value):
                            block = []
                            continue
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                            value = value[1:-1]
                        name = value
                        if user_named is not None:
                            break
        if block is not None and name is None:
            name = " ".join(part for part in block if part)
    except Exception:
        return False, ""
    return bool(user_named), name or ""


def _copilot_fallback_title(conn, sid: str, fields: dict) -> None:
    """Fill ``fields["title"]`` from the first turn, then the first checkpoint."""
    try:
        r = conn.execute(
            """
            SELECT user_message FROM turns
            WHERE session_id = ? AND user_message IS NOT NULL AND user_message != ''
            ORDER BY turn_index ASC LIMIT 1
            """,
            (sid,),
        ).fetchone()
        if r and r[0]:
            fields["title"] = clean_title(r[0])
            return
        r = conn.execute(
            """
            SELECT title FROM checkpoints
            WHERE session_id = ?
            ORDER BY checkpoint_number ASC LIMIT 1
            """,
            (sid,),
        ).fetchone()
        if r and r[0]:
            fields["title"] = clean_title(r[0])
    except Exception:
        pass


def parse_copilot():
    state_root = os.path.expanduser("~/.copilot/session-state")

    def enrich(conn, row, fields):
        sid = fields.get("session_id") or ""
        from_summary = bool(fields.get("title"))
        if not from_summary:
            _copilot_fallback_title(conn, sid, fields)
        try:
            user_named, yaml_name = _copilot_workspace_meta(state_root, sid)
        except Exception:
            user_named, yaml_name = False, ""
        if user_named:
            if not from_summary and yaml_name:
                fields["title"] = clean_title(yaml_name)
            if fields.get("title"):
                fields["title_source"] = "rename"
                return
        if from_summary:
            fields["title_source"] = "auto"

    return parse_sqlite(
        SqliteParserConfig(
            tool_name="copilot",
            agent="GitHub Copilot",
            db_path=os.path.expanduser("~/.copilot/session-store.db"),
            query="SELECT id, cwd, summary, updated_at, created_at FROM sessions",
            session_id=0,
            path=1,
            title=2,
            updated=3,
            created=4,
            require_path=True,
            enrich=enrich,
        )
    )


_DEVIN_DB = "~/.local/share/devin/cli/sessions.db"
# ``/rename <name>`` and ``/rename-chat <name>`` both rename; with no
# argument the name is typed into a prompt that ``prompt_history`` never
# sees, so only an inline name can be matched against the title.
_DEVIN_RENAME_RE = re.compile(r"^/rename(?:-chat)?(?:\s+(.*?))?\s*$", re.S)


def parse_devin():
    """Devin CLI sessions from ``~/.local/share/devin/cli/sessions.db``.

    ``sessions.title`` is filled either by Devin's auto-titler or by the
    user's ``/rename``; both write the same column. The only on-disk
    discriminator is ``prompt_history``, which records every prompt the
    user typed, ``/rename <name>`` included. A session whose latest
    inline ``/rename`` argument matches the title is a rename; a title
    that differs from the first prompt without one is the auto-titler's;
    a title equal to the first prompt (Devin's default) has no provenance.
    A rename typed into the interactive prompt leaves no argument on disk
    and so reads as auto, which under-marks rather than over-marks.

    ``prompt_history`` also holds prompts typed at the REPL before the
    session existed (``/usage``, ``/login-status``); only rows stamped at
    or after ``created_at`` belong to the session.
    """

    # Every session's prompts in one query on first use, instead of one
    # query per session. The ``created_at`` bound is applied in SQL, with
    # the same rule the per-row read used: a non-numeric value counts as 0.
    by_session: dict[str, list] = {}
    loaded: list[bool] = []

    def prompts_of(conn, sid):
        if not loaded:
            for session_id, content in conn.execute(
                "SELECT p.session_id, p.content FROM prompt_history p "
                "JOIN sessions s ON s.id = p.session_id "
                "WHERE p.is_shell = 0 AND p.timestamp >= CASE "
                "WHEN typeof(s.created_at) IN ('integer', 'real') "
                "THEN s.created_at ELSE 0 END "
                "ORDER BY p.session_id, p.timestamp, p.id"
            ):
                by_session.setdefault(session_id, []).append(content)
            loaded.append(True)
        return by_session.get(sid, ())

    def enrich(conn, row, fields):
        sid = fields.get("session_id")
        if not sid:
            return
        first_prompt = ""
        renamed_to = ""
        for content in prompts_of(conn, sid):
            text = str(content or "")
            match = _DEVIN_RENAME_RE.match(text)
            if match:
                renamed_to = clean_title(match.group(1) or "")
                continue
            if not first_prompt and text.strip():
                first_prompt = clean_title(text)
        title = fields.get("title") or ""
        if not title and first_prompt:
            fields["title"] = title = first_prompt
        if not title:
            return
        if renamed_to and renamed_to == title:
            fields["title_source"] = "rename"
        elif first_prompt and title != first_prompt:
            fields["title_source"] = "auto"

    return parse_sqlite(
        SqliteParserConfig(
            tool_name="devin",
            agent="Devin",
            db_path=os.path.expanduser(_DEVIN_DB),
            query="""
                SELECT id, working_directory, title, last_activity_at, created_at
                FROM sessions
                WHERE hidden = 0
            """,
            session_id=0,
            path=1,
            title=2,
            updated=3,
            created=4,
            require_path=True,
            enrich=enrich,
        )
    )


def parse_forge():
    def enrich(conn, row, fields):
        if fields.get("title"):
            return
        context = row[4] if len(row) > 4 else None
        if not context:
            return
        try:
            ctx = json.loads(context)
        except Exception:
            return
        for msg in ctx.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            inner = msg.get("message") if isinstance(msg.get("message"), dict) else msg
            text = inner.get("text") if isinstance(inner, dict) else None
            if isinstance(text, dict) and str(text.get("role", "")).lower() == "user":
                content = text.get("content") or ""
                m = re.search(r"<task>\s*(.*?)\s*</task>", content, re.S | re.I)
                fields["title"] = clean_title(m.group(1) if m else content)
                break

    return parse_sqlite(
        SqliteParserConfig(
            tool_name="forge",
            agent="Forge",
            db_path=os.path.expanduser("~/.forge/.forge.db"),
            query=(
                "SELECT conversation_id, title, created_at, updated_at, context "
                "FROM conversations"
            ),
            session_id=0,
            title=1,
            created=2,
            updated=3,
            require_path=False,
            default_path=os.path.expanduser("~"),
            enrich=enrich,
        )
    )


def parse_crush():
    """Crush: projects.json index + per-project crush.db (sqlite family + index)."""
    sessions: list[Session] = []
    projects_json = os.path.expanduser("~/.local/share/crush/projects.json")
    if not os.path.exists(projects_json):
        return sessions
    try:
        with open(projects_json) as f:
            data = json.load(f)
        projects = data.get("projects") if isinstance(data, dict) else data
        if not isinstance(projects, list):
            return sessions
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            path = proj.get("path") or ""
            data_dir = proj.get("data_dir") or ""
            if not path or not data_dir:
                continue
            db_path = os.path.join(os.path.expanduser(data_dir), "crush.db")
            if not os.path.exists(db_path):
                ts = parse_iso_ts(proj.get("last_accessed"))
                if ts:
                    sessions.append(
                        Session(
                            timestamp=ts,
                            agent="Crush",
                            path=path,
                            title="",
                            session_id="",
                            tool_name="crush",
                        )
                    )
                continue

            def enrich_path(conn, row, fields, _path=path):
                fields["path"] = _path

            sessions.extend(
                parse_sqlite(
                    SqliteParserConfig(
                        tool_name="crush",
                        agent="Crush",
                        db_path=db_path,
                        query="SELECT id, title, updated_at, created_at FROM sessions",
                        session_id=0,
                        title=1,
                        updated=2,
                        created=3,
                        require_path=False,
                        default_path=path,
                        enrich=enrich_path,
                    )
                )
            )
    except Exception as exc:
        # Recorded like every other parser: swallowed, a crash read as "no
        # Crush history" and the aggregator pruned its cached sessions.
        failures.record("crush", exc)
    return sessions


# ---------------------------------------------------------------------------
# JSONL family
# ---------------------------------------------------------------------------

# A transcript that ended by handing off has the marker as its last record,
# so a small tail is enough to find one. That is the whole test: a marker
# sitting behind more conversation is not in the tail to begin with.
_CLAUDE_HANDOFF_TAIL_BYTES = 8192


def _claude_transcript_paths(base: str) -> dict[str, str]:
    """Session id -> transcript path, across every project dir under ``base``.

    A ``Session`` carries the id and the cwd but not the file it came from,
    and the project dir name is a lossy encoding of that cwd, so the mapping
    is read off the directory rather than reconstructed. Two scandirs and no
    file reads.
    """
    out: dict[str, str] = {}
    mtimes: dict[str, float] = {}
    try:
        with os.scandir(base) as projects:
            for project in projects:
                if not project.is_dir() or not project.name.startswith("-"):
                    continue
                with os.scandir(project.path) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".jsonl"):
                            continue
                        sid = entry.name.removesuffix(".jsonl")
                        # A session resumed from a different cwd leaves a
                        # transcript under each project dir — the newest
                        # write is the live copy, not whichever scandir
                        # visited last.
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        if sid not in out or mtime >= mtimes.get(sid, -1):
                            out[sid] = entry.path
                            mtimes[sid] = mtime
    except Exception:
        return out
    return out


def _claude_handed_off(path: str) -> str:
    """The session ``path`` handed off to and then stopped, or "".

    Claude Code can continue a session in a fresh transcript, and writes
    ``{"type": "continued-in", "continuedInSessionId": ...}`` into the old
    file the moment it happens. That marker alone does not end the old
    session: work often carries on there afterwards, and both files are then
    real, separate sessions. One machine here has both shapes, a handoff
    with nothing after it and a handoff followed by 860 more turns.

    So a session is superseded only when no user or assistant record follows
    the marker. Records the harness writes for its own bookkeeping do not
    count; only conversation does.
    """
    try:
        from swe.sessions import zstd_copy

        # A compressed copy is a frame under the same name. The marker
        # sits in the plain transcript, so the tail is taken from that.
        plain = zstd_copy.plain(path)
        if plain is None:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                start = max(0, size - _CLAUDE_HANDOFF_TAIL_BYTES)
                fh.seek(start)
                chunk = fh.read().decode("utf-8", errors="ignore")
        else:
            size = len(plain)
            start = max(0, size - _CLAUDE_HANDOFF_TAIL_BYTES)
            chunk = plain[start:].decode("utf-8", errors="ignore")
    except OSError:
        parse_cache.note_read_error()
        return ""
    except Exception:
        return ""
    lines = chunk.split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # first line is a partial record
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind in ("user", "assistant"):
            return ""  # the conversation outlived the handoff
        if kind == "continued-in":
            successor = data.get("continuedInSessionId")
            return successor if isinstance(successor, str) and successor else ""
    return ""


def _account_homes(vendor: str) -> list[str]:
    """Each SWE account home for ``vendor``, sorted for stable order."""
    return [str(home) for home in account_homes_for(vendor)]


def parse_claude():
    def path_from_event(data: dict) -> str:
        # cwd often only appears as raw field in early lines
        return event_cwd(data, "cwd")

    def title_from_event(data: dict) -> str:
        if data.get("type") != "user":
            return ""
        msg = data.get("message") or {}
        return extract_user_text(msg.get("content"))

    # `/rename` appends {"type": "custom-title", "customTitle": ...} and Claude
    # Code's own auto-title appends {"type": "ai-title", "aiTitle": ...}, both
    # after the first prompt and re-emitted on later turns. A rename beats the
    # auto-title, which beats the first prompt.
    def tail_title_from_event(data: dict) -> tuple[int, str, str] | None:
        kind = data.get("type")
        if kind == "custom-title":
            return (2, str(data.get("customTitle") or ""), "rename")
        if kind == "ai-title":
            return (1, str(data.get("aiTitle") or ""), "auto")
        return None

    # The default home first, then every SWE account home: each holds
    # its own projects tree in the same layout, and sessions from one are
    # tagged with their home so their account can be named and resumed.
    roots = [(os.path.expanduser("~/.claude/projects/"), "", frozenset(), False)]
    roots += [
        (os.path.join(home, "projects"), home, frozenset(), False)
        for home in _account_homes("claude")
    ]

    def scan(base: str, skip=frozenset()) -> list[Session]:
        return parse_jsonl_projects(
            JsonlParserConfig(
                tool_name="claude",
                agent="Claude Code",
                base_dir=base,
                mode="nested_jsonl",
                project_filter=lambda name, _p: name.startswith("-"),
                path_from_event=path_from_event,
                path_from_project_dir=lambda name: path_from_encoded_dir(name, "-"),
                title_from_event=title_from_event,
                tail_title_from_event=tail_title_from_event,
                title_max_len=50,
                # One row per jsonl so multi-chat projects all appear in swe session
                one_session_per_file=True,
                require_path=True,
                skip_basenames=set(skip),
            )
        )

    sessions: list[Session] = []
    for base, account_home, _skip, _mirror in roots:
        batch = scan(base)
        if account_home:
            for sess in batch:
                sess.account_home = account_home
        sessions += batch

    # Transcripts swe mirrored before Claude Code pruned them. Only a
    # session no live file carries is added, so nothing lists twice.
    live = {(s.session_id, s.account_home) for s in sessions if s.session_id}
    mirror_roots = transcript_mirror.listing_roots("claude")
    for base, account_home, skip in mirror_roots:
        for sess in scan(base, skip):
            if sess.session_id and (sess.session_id, account_home) in live:
                continue
            sess.account_home = account_home
            sess.mirrored = True
            sessions.append(sess)
    roots += [(base, account_home, skip, True) for base, account_home, skip in mirror_roots]

    # The same session id resumed under a second project dir would list
    # twice: one conversation, one row — the newest timestamp is the
    # current copy. The dedup key carries the account home so a transcript
    # copied into another profile keeps one row per account.
    best_by_id: dict[tuple[str, str], int] = {}
    for i, sess in enumerate(sessions):
        sid = sess.session_id
        if not sid:
            continue
        key = (sid, sess.account_home)
        if key not in best_by_id or sess.timestamp > sessions[best_by_id[key]].timestamp:
            best_by_id[key] = i
    if len(best_by_id) != sum(1 for s in sessions if s.session_id):
        sessions = [
            s for i, s in enumerate(sessions)
            if not s.session_id or best_by_id[(s.session_id, s.account_home)] == i
        ]

    # One conversation continued across two transcripts is one session, and
    # both files carry the same title, so listing both reads as a duplicate.
    # The predecessor is dropped only when its successor is really on disk;
    # otherwise the whole conversation would vanish from the listing.
    # A handoff only chains transcripts inside one home, so the lookup is
    # scoped to the root the file came from.
    by_id = {
        (sess.session_id, sess.account_home): sess
        for sess in sessions if sess.session_id
    }
    superseded: set[tuple[str, str]] = set()
    for base, account_home, skip, is_mirror in roots:
        for session_id, file_path in _claude_transcript_paths(base).items():
            if (session_id, account_home) not in by_id:
                continue
            if is_mirror and ((session_id, account_home) in live
                              or f"{session_id}.jsonl" in skip):
                # A copy never speaks for a session a live file carries
                # (its marker may be stale), nor for a file still on disk.
                continue
            # The tail read depends on the file alone; whether the successor is
            # listed is decided fresh every run.
            successor = parse_cache.lookup(
                "claude:handoff",
                file_path,
                lambda fp=file_path: _claude_handed_off(fp),
                valid=lambda v: isinstance(v, str),
            )
            # A live session is superseded only by a live successor: a
            # copy of the successor proves nothing is on disk.
            known = live if (session_id, account_home) in live else by_id
            if successor and (successor, account_home) in known:
                superseded.add((session_id, account_home))
    if not superseded:
        return sessions
    return [
        s for s in sessions
        if (s.session_id, s.account_home) not in superseded
    ]


def parse_droid():
    def path_from_event(data: dict) -> str:
        if data.get("type") == "session_start":
            return data.get("cwd") or ""
        return ""

    # Line 1 is a ``session_start`` event rewritten in place. ``/rename``
    # sets ``isSessionTitleManuallySet: true``; otherwise droid's auto-titler
    # fills ``title`` after the first message or file edit. Older files only
    # carry the "New Session" placeholder, which falls through to the first
    # user prompt.
    def title_from_event(data: dict) -> str | tuple[str, str]:
        if data.get("type") == "session_start":
            start_title = str(data.get("title") or "").strip()
            if start_title and start_title.lower() != "new session":
                if data.get("isSessionTitleManuallySet") is True:
                    return (start_title, "rename")
                return (start_title, "auto")
            return ""
        if data.get("type") != "message":
            return ""
        msg = data.get("message") or {}
        if msg.get("role") != "user":
            return ""
        if msg.get("hookEventName") or msg.get("visibility") == "user_only":
            text = extract_user_text(msg.get("content"))
            if not text.strip():
                return ""
        return extract_user_text(msg.get("content"))

    return parse_jsonl_projects(
        JsonlParserConfig(
            tool_name="droid",
            agent="Droid",
            base_dir=os.path.expanduser("~/.factory/sessions/"),
            mode="nested_jsonl",
            path_from_event=path_from_event,
            path_from_project_dir=lambda name: path_from_encoded_dir(name, "-"),
            title_from_event=title_from_event,
            one_session_per_file=True,
            require_path=True,
        )
    )


# Rollout files are named ``rollout-<iso timestamp>-<thread uuid>.jsonl``.
# Resuming a thread writes a new file that carries its own rollout uuid after
# the thread's: ``rollout-<iso>-<thread uuid>_<rollout uuid>.jsonl``. The
# thread uuid — the first one — is the id the session index, the thread store
# and ``codex resume`` all use; the trailing one is the resume's own rollout.
_CODEX_ROLLOUT_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def codex_thread_id(session_id: str) -> str:
    """The thread uuid inside a codex session id, lower-cased, or "".

    A codex ``Session.session_id`` is the rollout file's stem, because that
    is what the transcript readers look a session up by. Everything else
    codex exposes — the session index, the thread store, ``codex resume`` —
    is keyed by the thread uuid, the first uuid in the stem: a resumed
    rollout appends its own ``_<rollout uuid>`` that only names the file.
    """
    match = _CODEX_ROLLOUT_ID_RE.search(session_id or "")
    return match.group(1).lower() if match else ""


# Codex's auto-titler runs the moment the first turn completes: across every
# thread on disk it landed 4-7 seconds after the thread's first name record,
# while every observed ``/name`` came 7 minutes to 2 hours later. A minute is
# well clear of both clusters.
_CODEX_AUTO_TITLE_WINDOW_MS = 60_000


def _codex_state_db(home: str = "") -> str:
    """Newest ``<home>/state_<n>.sqlite``, or "" when codex has none.

    ``home`` is the codex config dir — ``~/.codex`` by default, an SWE
    account home for an extra profile. The number in the filename is a
    schema version codex bumps on migration and the old file is left
    behind, so the highest one is the live store.
    """
    best, best_n = "", -1
    for path in glob.glob(
        os.path.expanduser(os.path.join(home or "~/.codex", "state_*.sqlite"))
    ):
        match = re.search(r"state_(\d+)\.sqlite$", path)
        if not match:
            continue
        version = int(match.group(1))
        if version > best_n:
            best, best_n = path, version
    return best


def _codex_thread_store(home: str = "") -> dict[str, tuple[str, str, bool, str]]:
    """Thread id -> ``(name, preview, worked, label)`` from the thread store.

    ``name`` is the thread's display name, whatever wrote it. ``preview`` is
    codex's record of the first message the *user* actually sent, which beats
    scanning the rollout for one: a transcript's first user-role item is
    often injected context (a plugin roster, an AGENTS.md) that the user
    never typed, and codex knows which was which.

    An empty ``preview`` alone does not prove a thread was never used: codex
    leaves it blank on spawned threads — subagents, automations — no matter
    how much work they did, because no user ever typed into them. ``worked``
    is the store's own ``tokens_used > 0``, codex's record that the thread
    ran; the only threads its listing hides (the ``idx_threads_visible_*``
    partial indexes filter ``preview <> ''``) *and* that earned nothing are
    ones opened and abandoned, and those are the only ones dropped.

    ``label`` names a spawned thread that has no prompt to name it by: the
    leaf of its ``agent_path`` (``/root/backend`` -> ``backend``), else its
    ``agent_nickname``.

    An unreadable or absent store yields an empty map, which turns both the
    naming and the hiding back off.
    """
    from swe.sessions.engines.common import open_sqlite_ro

    db_path = _codex_state_db(home)
    if not db_path:
        return {}
    conn = open_sqlite_ro(db_path)
    if conn is None:
        return {}
    out: dict[str, tuple[str, str, bool, str]] = {}
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if not {"id", "name", "preview"} <= cols:
            return {}
        preview_sql = (
            "COALESCE(NULLIF(preview, ''), first_user_message)"
            if "first_user_message" in cols else "preview"
        )
        tokens_sql = "tokens_used" if "tokens_used" in cols else "0"
        path_sql = "agent_path" if "agent_path" in cols else "NULL"
        nick_sql = "agent_nickname" if "agent_nickname" in cols else "NULL"
        for tid, name, preview, tokens, apath, nick in conn.execute(
            f"SELECT id, name, {preview_sql}, {tokens_sql}, "
            f"{path_sql}, {nick_sql} FROM threads"
        ):
            if not isinstance(tid, str) or not tid.strip():
                continue
            label = ""
            if isinstance(apath, str) and apath.strip("/"):
                label = apath.rstrip("/").rsplit("/", 1)[-1]
            elif isinstance(nick, str):
                label = nick.strip()
            out[tid.strip().lower()] = (
                name.strip() if isinstance(name, str) else "",
                preview.strip() if isinstance(preview, str) else "",
                bool(tokens) if isinstance(tokens, (int, float)) else False,
                label,
            )
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _codex_index_history(home: str = "") -> dict[str, list[tuple[float, str]]]:
    """Thread id -> chronological ``(ts_ms, name)`` records from the index.

    Codex keeps thread names outside the rollout transcript and appends
    ``{"id", "thread_name", "updated_at"}`` to
    ``~/.codex/session_index.jsonl`` every time one changes — and on some
    resume bookkeeping events too, re-emitting the name unchanged. The
    records stay in append order so each rollout can pick out the entries
    that existed while it ran. Ids are lower-cased so a lookup keyed off the
    rollout filename matches whatever case either side wrote; a record with
    an unreadable ``updated_at`` keeps its name with a 0 timestamp.
    """
    path = os.path.expanduser(
        os.path.join(home or "~/.codex", "session_index.jsonl")
    )
    records: dict[str, list[tuple[float, str]]] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("id")
                name = entry.get("thread_name")
                if not isinstance(sid, str) or not sid.strip():
                    continue
                if not isinstance(name, str) or not name.strip():
                    continue
                ts = parse_iso_ts(entry.get("updated_at")) or 0.0
                records.setdefault(sid.strip().lower(), []).append(
                    (ts, name.strip())
                )
    except Exception:
        return {}
    return records


def _codex_index_state(
    records: list[tuple[float, str]], rollout_ts: float
) -> tuple[list[str], float]:
    """``(names, span_ms)`` for the index state one rollout ran under.

    A resumed thread's index records span every resume it ever had, so the
    run ends at the entry closest to the rollout's own timestamp — later
    entries describe a thread this rollout predates — while entries written
    while the rollout was live always count even when an earlier one sits
    closer. A missing ``rollout_ts`` keeps the whole history.

    Within the kept run, consecutive repeats drop out because codex
    re-emits the current name on events that are not renames, and
    ``span_ms`` runs from the first record to the last one that introduced
    a name — so a resume that only re-states the name cannot stretch the
    rename window on its own.
    """
    if rollout_ts and records:
        past = -1
        near, best = 0, None
        for i, (ts, _name) in enumerate(records):
            if ts <= rollout_ts:
                past = i
            dist = abs(ts - rollout_ts)
            if best is None or dist < best:
                best, near = dist, i
        records = records[: max(past, near) + 1]
    names: list[str] = []
    first_ts = last_name_ts = 0.0
    for ts, name in records:
        if names and names[-1] == name:
            continue
        if not names:
            first_ts = ts
        names.append(name)
        last_name_ts = ts
    return names, last_name_ts - first_ts


def _codex_is_rename(names: list[str], span_ms: float, first_prompt: str) -> bool:
    """Whether a codex thread's current name was typed rather than generated.

    Three writers share the one ``thread_name`` field with no marker: codex
    stamps a prefix of the first prompt when the turn starts, replaces it
    with a generated title seconds later, and ``/name`` overwrites it
    whenever the user asks. What separates them is that codex only ever
    writes those two, in that order, and does so immediately:

    - A sequence longer than codex's own two entries has to end in a rename.
      When the first entry is not a prefix of the first prompt, codex skipped
      the placeholder and its budget is one entry, not two.
    - Codex's generated title lands within seconds of the placeholder. A last
      record a minute or more after the first one outlived that window, so a
      user wrote it.

    Both clauses are timing and shape arguments rather than a flag codex
    records, so this under-marks rather than over-marks: a rename that leaves
    a single record inside the window reads as a generated title. Across the
    threads this was calibrated on it marked every rename it claimed and
    missed none that had a second record.
    """
    if not names:
        return False
    prompt = (first_prompt or "").strip().lower()
    placeholder = bool(prompt) and prompt.startswith(names[0].strip().lower())
    if len(names) > (2 if placeholder else 1):
        return True
    return span_ms > _CODEX_AUTO_TITLE_WINDOW_MS


def parse_codex():
    def path_from_event(data: dict) -> str:
        if data.get("type") == "session_meta":
            payload = data.get("payload") or {}
            return payload.get("cwd") or ""
        return ""

    def title_from_event(data: dict) -> str:
        if data.get("type") != "response_item":
            return ""
        payload = data.get("payload") or {}
        if payload.get("type") != "message" or payload.get("role") != "user":
            return ""
        return extract_user_text(payload.get("content"))

    # Like claude, every SWE account home holds its own dated rollout
    # tree; sessions from one are tagged with it as their account home.
    # ``archived_sessions`` keeps archived threads flat rather than dated —
    # codex moves the rollout there, it does not delete it, and an archived
    # thread is still a session that ran.
    roots = [
        (os.path.expanduser("~/.codex/sessions/"), "", "*/*/*/*.jsonl"),
        (os.path.expanduser("~/.codex/archived_sessions/"), "", "*.jsonl"),
    ]
    roots += [
        (os.path.join(home, "sessions"), home, "*/*/*/*.jsonl")
        for home in _account_homes("codex")
    ]
    roots += [
        (os.path.join(home, "archived_sessions"), home, "*.jsonl")
        for home in _account_homes("codex")
    ]

    def scan(base: str, pattern: str, skip=frozenset()) -> list[Session]:
        return parse_jsonl_projects(
            JsonlParserConfig(
                tool_name="codex",
                agent="Codex CLI",
                base_dir=base,
                mode="glob",
                session_glob=pattern,
                path_from_event=path_from_event,
                title_from_event=title_from_event,
                title_max_len=80,
                require_path=True,
                skip_basenames=set(skip),
            )
        )

    sessions: list[Session] = []
    for base, account_home, pattern in roots:
        batch = scan(base, pattern)
        if account_home:
            for sess in batch:
                sess.account_home = account_home
        sessions += batch

    # Rollouts swe mirrored before Codex pruned them (the mirror keeps the
    # dated and the flat archived layouts as they were). A thread any live
    # rollout carries is already listed, so only the lost ones are added.
    def thread_key(sess: Session) -> tuple[str, str]:
        return (codex_thread_id(sess.session_id) or sess.session_id, sess.account_home)

    live = {thread_key(s) for s in sessions}
    for base, account_home, skip in transcript_mirror.listing_roots("codex"):
        for pattern in ("*/*/*/*.jsonl", "*.jsonl"):
            for sess in scan(base, pattern, skip):
                sess.account_home = account_home
                if thread_key(sess) in live:
                    continue
                sess.mirrored = True
                sessions.append(sess)

    # Resuming a thread writes another rollout file, and every file of one
    # thread carries the same thread uuid in its stem — so the files collapse
    # to one session per thread. The newest rollout wins: it holds the
    # resumed conversation and is where the thread last moved. Files with no
    # uuid have nothing to collapse on and stay one row each.
    freshest: dict[tuple[str, str], int] = {}
    tids = [codex_thread_id(sess.session_id) for sess in sessions]
    for i, tid in enumerate(tids):
        if not tid:
            continue
        key = (tid, sessions[i].account_home)
        if key not in freshest or sessions[i].timestamp > sessions[freshest[key]].timestamp:
            freshest[key] = i
    paired = [
        (tid, sess)
        for i, (tid, sess) in enumerate(zip(tids, sessions))
        if not tid or freshest[(tid, sess.account_home)] == i
    ]

    # A codex thread's name lives outside its rollout, in two places that say
    # different halves of the truth: the store holds the current name and
    # whether the thread was ever used, the index holds the history that says
    # whether a name was typed or generated. Either being unreadable degrades
    # to what the other knows, and a thread neither has heard of keeps the
    # first-prompt title the transcript scan produced. Each profile home
    # keeps its own store and index, so both are read per account home.
    by_home = {
        home: (
            _codex_thread_store(home),
            _codex_index_history(home),
        )
        for home in {sess.account_home for _, sess in paired}
    }
    if not any(store or history for store, history in by_home.values()):
        return [sess for _, sess in paired]

    kept: list[Session] = []
    for tid, sess in paired:
        store, history = by_home.get(sess.account_home, ({}, {}))
        stored_name, preview, worked, label = store.get(
            tid, ("", "", False, "")
        )
        # Only the store can say a thread was never used, so a thread it has
        # never heard of is kept rather than guessed at. "Never used" takes
        # the token ledger agreeing: a spawned thread runs with no user ever
        # typing into it, so an empty preview alone would hide every
        # subagent and automation codex ran.
        if tid in store and not preview and not worked:
            continue
        names, span_ms = _codex_index_state(history.get(tid, []), sess.timestamp)
        # The store holds the live name; the index is the audit trail behind
        # it and stands in when the store has none. Next the spawned label —
        # a subagent has no prompt to title from. With no name at all,
        # codex's own note of the first user message still beats the
        # transcript scan, which cannot tell a typed prompt from injected
        # context. That fallback is nobody's chosen title, so it stays
        # unmarked however the name above it was written.
        title = clean_title(
            stored_name or (names[-1] if names else "") or label, 80
        )
        if title:
            sess.title = title
            if _codex_is_rename(names, span_ms, preview):
                sess.title_source = "rename"
        elif preview:
            fallback = clean_title(preview, 80)
            if fallback:
                sess.title = fallback
        kept.append(sess)
    return kept


def parse_pi():
    def path_from_event(data: dict) -> str:
        if data.get("type") == "session":
            return data.get("cwd") or ""
        return event_cwd(data, "cwd", "workspace")

    def title_from_event(data: dict) -> str:
        if data.get("type") != "message":
            return ""
        msg = data.get("message") or {}
        if msg.get("role") != "user":
            return ""
        return extract_user_text(msg.get("content"))

    def project_filter(name: str, _path: str) -> bool:
        return name.startswith("--") and name.endswith("--")

    def path_from_project_dir(name: str) -> str:
        inner = name[2:-2] if name.startswith("--") and name.endswith("--") else name
        if inner.startswith("-"):
            inner = inner[1:]
        return "/" + inner.replace("-", "/")

    # `/name <name>` (and `pi --name`) appends {"type": "session_info",
    # "name": ...} after the first prompt; it can be repeated and pi's own
    # reader takes the latest one. Pi has no auto-titler, so every
    # session_info name is user-set. Limitation: pi clears a name with
    # {"name": ""}, but the engine drops empty text, so a clear that follows
    # an earlier name still surfaces that earlier name as a rename.
    def tail_title_from_event(data: dict) -> tuple[int, str, str] | None:
        if data.get("type") != "session_info":
            return None
        name = data.get("name")
        if not isinstance(name, str):
            return None
        name = name.strip()
        if not name:
            return None
        return (2, name, "rename")

    return parse_jsonl_projects(
        JsonlParserConfig(
            tool_name="pi",
            agent="Pi CLI",
            base_dir=os.path.expanduser("~/.pi/agent/sessions/"),
            mode="nested_jsonl",
            project_filter=project_filter,
            path_from_event=path_from_event,
            path_from_project_dir=path_from_project_dir,
            title_from_event=title_from_event,
            tail_title_from_event=tail_title_from_event,
            title_max_len=50,
            one_session_per_file=True,
            session_id_from_path=lambda fp: fp,  # historical: full path as id
            require_path=True,
        )
    )


def _cursor_meta(session_id: str) -> dict:
    """The Cursor CLI's own record for a session, or {} when there is none.

    The CLI writes ``~/.cursor/chats/<workspace-hash>/<session-id>/meta.json``
    with ``cwd``, ``title``, ``createdAtMs`` and ``updatedAtMs``. The
    workspace hash is opaque, so the file is found by scanning one level.
    """
    chats_dir = os.path.expanduser("~/.cursor/chats")
    if not session_id or not os.path.isdir(chats_dir):
        return {}
    try:
        with os.scandir(chats_dir) as it:
            for entry in it:
                meta_path = os.path.join(entry.path, session_id, "meta.json")
                if not os.path.isfile(meta_path):
                    continue
                with open(meta_path) as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _tool_paths_dir(tool_paths: list[str]) -> str:
    """A directory-shaped path from the first tool-call paths, or "".

    The common prefix of two or more distinct paths is a directory by
    construction. When the prefix is one of the paths itself (a single call,
    or the same file repeated) it is the file the agent touched, so its parent
    is used. Existence is checked where it can be, but a project that has
    since moved still yields a directory rather than a file.
    """
    if not tool_paths:
        return ""
    try:
        common = os.path.commonpath(tool_paths)
    except ValueError:
        return ""
    if not common:
        return ""
    if os.path.isdir(common):
        return common
    if os.path.isfile(common) or common in tool_paths:
        return os.path.dirname(common)
    return common


def _under(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or lives inside it."""
    if not path or not root:
        return False
    root = root.rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def _cursor_session_dir(
    enc_entry_path: str, tool_paths: list[str], meta: dict
) -> str:
    """Directory a Cursor session ran in. Never a file.

    Signals in priority order:

    1. ``meta["cwd"]``, recorded by the Cursor CLI in the session's meta.json.
    2. The common prefix of the first tool-call paths, collapsed to a
       directory. Anything under ``~/.cursor`` is the harness's own config
       and is kept only as a last resort.
    3. The encoded project folder name (``Users-foo-bar`` -> ``/Users/foo/bar``)
       when that path exists. Decoding is lossy, so it is only trusted when
       the directory is really there. An existing decoded folder outranks a
       tool-path directory that no longer exists.
    4. ``enc_entry_path`` itself, so nothing regresses.
    """
    cwd = meta.get("cwd", "") if isinstance(meta, dict) else ""
    if isinstance(cwd, str) and cwd.strip():
        cwd = cwd.strip()
        if os.path.isfile(cwd):
            cwd = os.path.dirname(cwd)
        return cwd

    cursor_home = os.path.expanduser("~/.cursor")
    tool_dir = _tool_paths_dir(tool_paths)
    tool_dir_is_project = bool(tool_dir) and not _under(tool_dir, cursor_home)
    if tool_dir_is_project and os.path.isdir(tool_dir):
        return tool_dir

    decoded = path_from_encoded_dir(os.path.basename(enc_entry_path), strip_prefix="")
    if decoded and os.path.isdir(decoded) and not _under(decoded, cursor_home):
        return decoded

    if tool_dir_is_project:
        return tool_dir
    if tool_dir and os.path.isdir(tool_dir):
        return tool_dir
    return enc_entry_path


_CURSOR_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL)
_CURSOR_USER_QUERY_RE = re.compile(r"</?user_query>")


def _cursor_meta_title(meta: dict) -> str | None:
    """The CLI's own title from ``meta.json``, cleaned, or None when unset."""
    meta_title = meta.get("title") if isinstance(meta, dict) else None
    if isinstance(meta_title, str) and meta_title.strip():
        return clean_title(meta_title, 80)
    return None


def _cursor_session_title(first_user_text: str, meta: dict) -> str:
    """Title for a Cursor session.

    Prefer the CLI's own ``title`` from ``meta.json``. Otherwise the Cursor
    CLI wraps each stored prompt as ``<timestamp>...</timestamp>`` followed
    by ``<user_query>...</user_query>``; drop the timestamp element with its
    content and unwrap the query before the usual cleaning. Legacy
    transcripts without the wrapper pass straight through ``clean_title``.
    """
    from_meta = _cursor_meta_title(meta)
    if from_meta is not None:
        return from_meta
    text = _CURSOR_TIMESTAMP_RE.sub("", first_user_text or "")
    text = _CURSOR_USER_QUERY_RE.sub("", text)
    return clean_title(text, 80)


def _cursor_scan(jsonl_path: str, home: str) -> list:
    """``[prompt title, tool-call paths under home]`` from one transcript.

    The title is what ``_cursor_session_title`` makes of the first user
    prompt with no meta.json to go on; meta.json is read every run and wins
    over it. Stops once it has the prompt and five paths. A malformed line
    ends the scan and keeps whatever was gathered before it.
    """
    first_user_text = ""
    all_paths: list[str] = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                role = data.get("role", "")
                content = data.get("message", {}).get("content", "")
                if role == "user" and not first_user_text:
                    first_user_text = extract_user_text(content)
                if isinstance(content, list):
                    for block in content:
                        inp = block.get("input", {})
                        if isinstance(inp, dict):
                            p = inp.get("path", "")
                            if p and p.startswith(home):
                                all_paths.append(p)
                if first_user_text and len(all_paths) >= 5:
                    break
    except OSError:
        parse_cache.note_read_error()
    except Exception:
        pass
    return [_cursor_session_title(first_user_text, {}), all_paths]


def _is_cursor_scan(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], list)
        and all(isinstance(p, str) for p in value[1])
    )


def _cursor_transcript_sessions() -> list[Session]:
    """Sessions from ``~/.cursor/projects/*/agent-transcripts`` JSONL files."""
    sessions: list[Session] = []
    projects_dir = os.path.expanduser("~/.cursor/projects")
    if not os.path.exists(projects_dir):
        return sessions
    home = os.path.expanduser("~")
    try:
        # ⚡ Bolt: Using os.scandir to reduce stat syscalls
        with os.scandir(projects_dir) as enc_entry_it:
            for enc_entry in enc_entry_it:
                transcripts_dir = os.path.join(enc_entry.path, "agent-transcripts")
                if not os.path.isdir(transcripts_dir):
                    continue
                with os.scandir(transcripts_dir) as uuid_entry_it:
                    for uuid_entry in uuid_entry_it:
                        if not uuid_entry.is_dir():
                            continue
                        jsonl_path = os.path.join(uuid_entry.path, f"{uuid_entry.name}.jsonl")
                        if not os.path.isfile(jsonl_path):
                            continue
                        mtime = get_mtime(jsonl_path)
                        if mtime == 0:
                            continue
                        # The transcript scan is cached per file; meta.json
                        # and the directory checks below run every time.
                        prompt_title, all_paths = parse_cache.lookup(
                            "cursor:scan",
                            jsonl_path,
                            lambda fp=jsonl_path: _cursor_scan(fp, home),
                            valid=_is_cursor_scan,
                        )
                        meta = _cursor_meta(uuid_entry.name)
                        title = _cursor_meta_title(meta)
                        sessions.append(
                            Session(
                                timestamp=mtime,
                                agent="Cursor",
                                path=_cursor_session_dir(enc_entry.path, all_paths, meta),
                                title=prompt_title if title is None else title,
                                session_id=uuid_entry.name,
                                tool_name="cursor",
                            )
                        )
    except Exception as exc:
        failures.record("cursor", exc)
    return sessions


def _cursor_store_sessions(seen: set[str]) -> list[Session]:
    """Sessions whose transcript is a ``store.db`` (ACP runs, ``chats``).

    ``cursor_store`` owns the format. An id already listed by the
    agent-transcripts scan keeps that row; the store copy is the same
    session's own record, not a second session.
    """
    sessions: list[Session] = []
    try:
        for sid, session_dir in cursor_store.session_dirs():
            if sid in seen:
                continue
            seen.add(sid)
            meta = cursor_store.read_meta_json(session_dir)
            title = _cursor_meta_title(meta)
            cwd = meta.get("cwd")
            store_meta: dict = {}
            workspace = ""
            if title is None or not (isinstance(cwd, str) and cwd.strip()):
                store_meta, workspace = cursor_store.session_fields(
                    os.path.join(session_dir, "store.db")
                )
                if title is None:
                    title = _cursor_meta_title({"title": store_meta.get("name")})
            if isinstance(cwd, str) and cwd.strip():
                cwd = cwd.strip()
                if os.path.isfile(cwd):
                    cwd = os.path.dirname(cwd)
            else:
                cwd = workspace
            sessions.append(
                Session(
                    timestamp=cursor_store.store_stamp(session_dir, meta, store_meta),
                    agent="Cursor",
                    path=cwd,
                    title=title or "",
                    session_id=sid,
                    tool_name="cursor",
                )
            )
    except Exception as exc:
        failures.record("cursor", exc)
    return sessions


def parse_cursor():
    """Cursor agent-transcripts layout is unique; keep custom scanner."""

    def custom() -> list[Session]:
        sessions = _cursor_transcript_sessions()
        sessions.extend(_cursor_store_sessions({s.session_id for s in sessions}))
        return sessions

    return parse_jsonl_projects(
        JsonlParserConfig(
            tool_name="cursor",
            agent="Cursor",
            base_dir="~/.cursor/projects",
            custom=custom,
        )
    )


def _freebuff_run_state(state_path: str) -> list:
    """``[cwd, title]`` from a freebuff ``run-state.json``; either may be None.

    A malformed file keeps whatever was read before the failure, the way
    the inline version this replaced did.
    """
    cwd = None
    title = None
    try:
        with open(state_path) as f:
            state = json.load(f)
        mas = state.get("sessionState", {}).get("mainAgentState", {}) or {}
        cwd = mas.get("cwd") or mas.get("initialCwd")
        for msg in mas.get("messageHistory", []) or []:
            if msg.get("role") != "user":
                continue
            text = extract_user_text(msg.get("content"))
            if text:
                title = clean_title(text, 50)
                break
    except OSError:
        parse_cache.note_read_error()
    except Exception:
        pass
    return [cwd, title]


def parse_freebuff():
    base = os.path.expanduser("~/.config/manicode/projects/")

    def enrich_session(fields: dict, sess_dir: str, project_name: str) -> None:
        if not fields.get("path"):
            fields["path"] = os.path.join(base, project_name)
        state_path = os.path.join(sess_dir, "run-state.json")
        if not os.path.exists(state_path):
            return
        # run-state.json is cached under its own path and signature, so a
        # change to it is seen even when log.jsonl did not move.
        cwd, title = parse_cache.lookup(
            "freebuff:run-state",
            state_path,
            lambda: _freebuff_run_state(state_path),
            valid=lambda v: (
                isinstance(v, list)
                and len(v) == 2
                and (v[1] is None or isinstance(v[1], str))
            ),
        )
        if cwd:
            fields["path"] = cwd
        if title is not None:
            fields["title"] = title

    return parse_jsonl_projects(
        JsonlParserConfig(
            tool_name="freebuff",
            agent="Freebuff",
            base_dir=base,
            mode="session_dirs",
            chats_subdir="chats",
            primary_files={"log.jsonl"},
            enrich_session=enrich_session,
            require_path=False,
            default_path="",
        )
    )


# ---------------------------------------------------------------------------
# JSON family
# ---------------------------------------------------------------------------

def parse_continue():
    sessions_dir = os.path.expanduser("~/.continue/sessions")
    index_path = os.path.join(sessions_dir, "sessions.json")

    def enrich(fields, entry, file_path):
        sid = fields.get("session_id") or ""
        session_file = os.path.join(sessions_dir, f"{sid}.json") if sid else ""
        if session_file and os.path.exists(session_file):
            fields["timestamp"] = max(fields["timestamp"], get_mtime(session_file))
            if not fields.get("title") or fields["title"].lower() in (
                "untitled session",
                "new chat session open",
            ):
                try:
                    with open(session_file) as sf:
                        data = json.load(sf)
                    for item in data.get("history") or []:
                        msg = item.get("message") if isinstance(item, dict) else None
                        if not isinstance(msg, dict) or msg.get("role") != "user":
                            continue
                        text = extract_user_text(msg.get("content"))
                        if text.strip():
                            fields["title"] = clean_title(text)
                            break
                except Exception:
                    pass

    return parse_json_store(
        JsonParserConfig(
            tool_name="continue",
            agent="Continue",
            mode="index",
            index_path=index_path,
            index_items=lambda data: data if isinstance(data, list) else [],
            get_id=lambda e, _f: str(e.get("sessionId") or e.get("id") or ""),
            get_path=lambda e, _f: strip_file_uri(
                e.get("workspaceDirectory") or e.get("cwd") or ""
            ),
            get_title=lambda e, _f: e.get("title") or "",
            get_ts=lambda e, _f: parse_iso_ts(
                e.get("dateCreated") or e.get("date") or 0
            ),
            require_path=True,
            enrich=enrich,
        )
    )


def parse_cline():
    # Cline 3.x writes one directory per session under data/sessions:
    # <id>.json holds metadata (session_id, started_at, cwd, prompt,
    # metadata.title) and <id>.messages.json holds the transcript — the
    # "session_id" key separates the two. Older installs keep a
    # taskHistory.json index under data/state, so read both.
    sessions = parse_json_store(
        JsonParserConfig(
            tool_name="cline",
            agent="Cline",
            mode="files",
            base_dir=os.path.expanduser("~/.cline/data/sessions"),
            file_glob="*/*.json",
            include=lambda e, _f: isinstance(e, dict) and "session_id" in e,
            get_id=lambda e, _f: str(e.get("session_id") or ""),
            get_path=lambda e, _f: e.get("cwd") or e.get("workspace_root") or "",
            get_title=lambda e, _f: (
                (e.get("metadata") or {}).get("title") or e.get("prompt") or ""
            ),
            get_ts=lambda e, _f: parse_iso_ts(e.get("started_at")),
            require_path=True,
            # The glob also matches every <id>.messages.json transcript,
            # which can run to megabytes: parse each file once per change.
            cache=True,
        )
    )
    sessions += parse_json_store(
        JsonParserConfig(
            tool_name="cline",
            agent="Cline",
            mode="index",
            index_path=os.path.expanduser("~/.cline/data/state/taskHistory.json"),
            index_items=lambda data: data if isinstance(data, list) else [],
            get_id=lambda e, _f: str(e.get("id") or e.get("ulid") or ""),
            get_path=lambda e, _f: e.get("cwdOnTaskInitialization") or e.get("cwd") or "",
            get_title=lambda e, _f: e.get("task") or "",
            get_ts=lambda e, _f: parse_iso_ts(e.get("ts") or e.get("id") or 0),
            require_path=True,
        )
    )
    return sessions


def _grok_flag_is_true(value) -> bool:
    """summary.json ``title_is_manual`` is a bool; accept "true"/"1" defensively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1")
    return False


def _grok_title_source(entry) -> str:
    """"rename" for a manual /rename, "auto" for a generated title, else ""."""
    if not isinstance(entry, dict):
        return ""
    generated = entry.get("generated_title")
    summary = entry.get("session_summary")
    title = generated or summary or ""
    if not isinstance(title, str) or not title.strip():
        return ""
    if _grok_flag_is_true(entry.get("title_is_manual")):
        return "rename"
    if isinstance(generated, str) and generated.strip():
        return "auto"
    return ""


def parse_grok():
    def path_from_parent(enc: str) -> str:
        decoded = unquote(enc)
        if decoded.startswith("/"):
            return decoded
        if decoded.startswith("Users/"):
            return "/" + decoded
        return ""

    def get_title(entry, _fp):
        return entry.get("generated_title") or entry.get("session_summary") or ""

    def get_ts(entry, _fp):
        return (
            parse_iso_ts(entry.get("last_active_at"))
            or parse_iso_ts(entry.get("updated_at"))
            or parse_iso_ts(entry.get("created_at"))
            or 0
        )

    def enrich(fields, entry, file_or_dir):
        sess_dir = (
            os.path.dirname(file_or_dir)
            if os.path.isfile(file_or_dir)
            else file_or_dir
        )
        ctx_path = os.path.join(sess_dir, "prompt_context.json")
        if os.path.exists(ctx_path):
            try:
                with open(ctx_path) as f:
                    ctx = json.load(f)
                cwd = ctx.get("working_directory") or ""
                if cwd:
                    fields["path"] = cwd
            except Exception:
                pass
        if not fields.get("path"):
            fields["path"] = os.path.expanduser("~")
        chat_path = os.path.join(sess_dir, "chat_history.jsonl")
        ts = fields.get("timestamp") or 0
        for candidate in (chat_path, sess_dir):
            ts = max(ts, get_mtime(candidate))
        if ts:
            fields["timestamp"] = ts
        if fields.get("title"):
            fields["title_source"] = _grok_title_source(entry)

    return parse_json_store(
        JsonParserConfig(
            tool_name="grok",
            agent="Grok",
            mode="nested_dirs",
            base_dir=os.path.expanduser("~/.grok/sessions"),
            session_file="summary.json",
            path_from_parent=path_from_parent,
            get_title=get_title,
            get_ts=get_ts,
            enrich=enrich,
            require_path=False,
            default_path=os.path.expanduser("~"),
        )
    )


def parse_gemini():
    def session_dir_from_item(path_key, hash_dir):
        if not hash_dir:
            return ""
        return os.path.expanduser(f"~/.gemini/tmp/{hash_dir}")

    def enrich(fields, entry, file_or_dir):
        sess_dir = (
            os.path.dirname(file_or_dir)
            if os.path.isfile(file_or_dir)
            else file_or_dir
        )
        log_path = os.path.join(sess_dir, "logs.json")
        if not os.path.exists(log_path):
            return
        try:
            with open(log_path) as lf:
                logs = json.load(lf)
            for item in logs:
                if item.get("type") == "user":
                    msg = (item.get("message") or "").strip()
                    if msg:
                        fields["title"] = clean_title(msg, 50)
                        break
        except Exception:
            pass

    def map_items(data):
        projects = data.get("projects", {}) if isinstance(data, dict) else {}
        return list(projects.items())

    def get_ts(entry, file_or_dir):
        sess_dir = (
            os.path.dirname(file_or_dir)
            if os.path.isfile(file_or_dir)
            else file_or_dir
        )
        ts = 0.0
        try:
            # ⚡ Bolt: Using os.scandir to reduce stat syscalls
            with os.scandir(sess_dir) as name_entry_it:
                for name_entry in name_entry_it:
                    ts = max(ts, get_mtime(name_entry.path))
        except Exception:
            pass
        return ts or get_mtime(sess_dir)

    return parse_json_store(
        JsonParserConfig(
            tool_name="gemini",
            agent="Gemini CLI",
            mode="project_map",
            index_path=os.path.expanduser("~/.gemini/projects.json"),
            session_file="",  # no primary JSON; enrich reads logs.json
            map_items=map_items,
            session_dir_from_item=session_dir_from_item,
            get_id=lambda e, _f: str(e.get("_key") or ""),
            get_path=lambda e, _f: str(e.get("_key") or e.get("path") or ""),
            get_ts=get_ts,
            require_path=True,
            enrich=enrich,
        )
    )
