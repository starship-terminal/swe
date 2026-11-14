"""The Sessions page's rows (ports ``Sessions.swift``, ``SessionFilter.swift``
and ``SessionProjects.swift``).

Rows are the hub's catalog (``/v1/sessions``: this machine plus every
satellite), each joined with its ledger usage by ``(harness, id)``, a
project name and whether it looks accidental. Filtering, grouping and
the summary happen in the page, as in the app.
"""

from __future__ import annotations

import os
from pathlib import Path

from swe.ledger.sessions import merge
from swe.sessions.parsers import codex_thread_id

# Harnesses whose sessions leave usage in a log some ledger reads. Only
# those can be judged accidental (SessionFilter.usageLogHarnesses).
_USAGE_LOG = {"claude", "codex", "opencode", "devin", "pi", "droid", "cline", "cursor", "grok-bot"}
# Harnesses with no per-turn timing, judged on tokens alone.
_NO_TIMING = {"cursor", "droid", "cline"}
# Harnesses this ledger reads (SessionFilter.localLogHarnesses). A row of
# one of these with no usage at all, once the ledger has scanned, is an
# accidental session; cursor and grok-bot are feed-only, so a session the
# poll never covered is not judged empty.
LEDGER_HARNESSES = {"claude", "codex", "opencode", "devin", "pi", "droid", "cline"}

_CLOUD = {"grok-bot", "devin-cloud"}


def is_trivial(harness: str, usage: dict | None, scanned: bool,
               covered: bool = True) -> bool:
    """A short or empty session the page hides by default: no tokens, or
    at most two responses inside a minute with under 100 output tokens.
    With no usage at all the judgement needs a scan that actually reached
    this row's source — ``covered`` (``ledger_covers``), not just the
    harness being one the ledger reads."""
    if harness not in _USAGE_LOG:
        return False
    if usage is not None:
        if usage["tokens"] == 0 or usage["events"] == 0:
            return True
        if harness in _NO_TIMING:
            return False
        return (usage["events"] <= 2
                and usage["last_ts"] - usage["first_ts"] < 60
                and usage["output"] + usage["reasoning"] < 100)
    return scanned and covered and harness in LEDGER_HARNESSES


def ledger_covers(row: dict, home: Path | None = None,
                  droid_ids: dict | None = None) -> bool:
    """Whether the ledger's source walk reaches this row's own file.

    The catalog is wider than the ledger for two harnesses:
    ``parse_cline`` also lists the legacy ``data/state/taskHistory.json``
    index the scan never reads, and ``parse_droid`` a transcript whose
    ``.settings.json`` sibling never existed. A row from one of those
    sources is unread, not accidental, and stays visible. ``parse_pi``
    and the ledger read the same ``agent/sessions`` tree, so every pi
    row is covered by definition.

    ``droid_ids`` is a cache one listing shares across its rows: each
    Droid sessions folder is listed once, not globbed per row."""
    harness = str(row.get("harness") or "")
    if harness not in ("cline", "droid"):
        return True
    sid = str(row.get("id") or "")
    if not sid or os.sep in sid or (os.altsep and os.altsep in sid):
        return False
    account = row.get("account_home")
    if account:
        base = Path(account)
    else:
        home = Path(home) if home is not None else Path.home()
        base = home / (".cline" if harness == "cline" else ".factory")
    if harness == "cline":
        # The scan reads ``data/sessions/<id>/<id>.json`` only; the
        # taskHistory index alone does not cover the row.
        return (base / "data" / "sessions" / sid / f"{sid}.json").is_file()
    # ``<project>/<sid>.settings.json`` beside the session's transcript.
    if droid_ids is None:
        droid_ids = {}
    ids = droid_ids.get(base)
    if ids is None:
        suffix = ".settings.json"
        ids = droid_ids[base] = {
            p.name[:-len(suffix)]
            for p in (base / "sessions").glob(f"*/*{suffix}")}
    return sid in ids


def location(harness: str, session_id: str | None) -> str:
    sid = session_id or ""
    if harness in _CLOUD or (harness == "cursor" and sid.startswith(("bc-", "sand-"))):
        return "Cloud"
    return "Local"


# ---------------------------------------------------------------- projects

def is_hash_like(name: str) -> bool:
    """A generated id rather than a project: 7+ letters and digits, at
    least 80% hex (a commit, a UUID, a worktree hash)."""
    word = [c for c in name if c.isalnum()]
    if len(word) < 7:
        return False
    hexes = sum(c in "0123456789abcdefABCDEF" for c in word)
    return hexes >= 0.8 * len(word)


def _main_repo_root(contents: str, folder: str) -> str | None:
    line = next((ln for ln in contents.splitlines() if ln.startswith("gitdir:")), None)
    if line is None:
        return None
    path = line[len("gitdir:"):].strip()
    if not path.startswith("/"):
        path = os.path.join(folder, path)
    path = os.path.normpath(path)
    if "/.git/" in path:
        return path[:path.index("/.git/")]
    if path.endswith("/.git"):
        return path[:-len("/.git")]
    return None


def _root(cwd: str) -> str | None:
    folder = cwd.rstrip("/") or cwd
    while folder and folder != "/":
        git = Path(folder) / ".git"
        if git.is_dir():
            return folder
        if git.is_file():
            try:
                contents = git.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return folder
            return _main_repo_root(contents, folder) or folder
        folder = os.path.dirname(folder)
    return None


def _pattern(cwd: str) -> str | None:
    """The repository a gone worktree path belonged to, by the shapes
    harnesses keep worktrees under."""
    parts = [p for p in cwd.split("/") if p]
    for i, part in enumerate(parts):
        if i == 0:
            continue
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        if part == ".claude" and nxt == "worktrees":
            return parts[i - 1]
        if part == ".codex" and nxt == "worktrees":
            return parts[i + 3] if i + 3 < len(parts) else None
        if part in {".worktrees", "worktrees"}:
            return parts[i - 1]
    return None


def project_of(cwd: str | None, local: bool = True) -> str | None:
    """The repository a folder belongs to (two worktrees of one repo
    share it), else a worktree-shape guess, else the folder's name.
    ``local=False`` is a path on another machine: this disk says nothing
    about it, so only the path's shape counts."""
    if not cwd:
        return None
    root = _root(cwd) if local else None
    if root:
        return os.path.basename(root.rstrip("/")) or None
    return _pattern(cwd) or (os.path.basename(cwd.rstrip("/")) or None)


# ---------------------------------------------------------------- rows

def _codex_threads(usage: dict[tuple[str, str], dict]) -> dict[str, dict]:
    """Codex usage per thread. A resumed thread writes a new rollout
    (``…-<thread>_<rollout>``) and swe lists only the newest, so the
    row's usage is every rollout of its thread. A response the resume
    repeats is one ledger row (``token_usage_record`` keys on the response
    id), so merging counts it once; what older ``token_count`` logs replay
    is whatever the ledger itself counted, as in the app's ledger."""
    parts: dict[str, list[dict]] = {}
    for (harness, session), u in usage.items():
        thread = codex_thread_id(session) if harness == "codex" else ""
        if thread:
            parts.setdefault(thread, []).append(u)
    return {t: (p[0] if len(p) == 1 else merge(p)) for t, p in parts.items()}


def _usage_for(harness: str, sid: str, usage: dict, threads: dict) -> dict | None:
    if harness == "codex":
        thread = codex_thread_id(sid)
        if thread in threads:
            return threads[thread]
    return usage.get((harness, sid))


def build(rows: list[dict], usage: dict[tuple[str, str], dict], hub_machine_id: str,
          machines: dict[str, dict], scanned: bool,
          home: Path | None = None) -> list[dict]:
    """``scanned`` is a clean ledger scan: a session with no usage after
    one is accidental; after a scan that couldn't read every source it
    may only be unread, so it stays visible. ``home`` is the machine's
    home dir for ``ledger_covers``'s source checks."""
    projects: dict[tuple[bool, str | None], str | None] = {}
    droid_ids: dict = {}
    threads = _codex_threads(usage)
    out = []
    for row in rows:
        harness = str(row.get("harness") or "")
        sid = row.get("id")
        here = row.get("machine_id") == hub_machine_id and not row.get("remote")
        # The ledger reads this machine's logs only; a satellite's rows
        # have no usage here, so they are never judged accidental either.
        u = _usage_for(harness, sid, usage, threads) if here and sid else None
        cwd = row.get("cwd") or (u or {}).get("cwd")
        if (here, cwd) not in projects:
            projects[(here, cwd)] = project_of(cwd, local=here)
        project = projects[(here, cwd)]
        machine = machines.get(str(row.get("machine_id") or "")) or {}
        # The harness deleted the transcript; swe lists its read-only copy
        # (#1918). Nothing can resume, attach or fork it, and with its
        # source gone the ledger has no usage for it, which must not read
        # as an accidental empty session.
        mirrored = bool(row.get("mirrored"))
        out.append({
            "key": row.get("key"),
            "id": sid,
            "harness": harness,
            "agent": row.get("agent") or harness,
            "title": row.get("title") or "",
            "cwd": cwd,
            "project": project,
            "project_key": ("other" if project and is_hash_like(project)
                            else (project or "").lower() or None),
            "last_active_at": row.get("last_active_at") or 0,
            "status": row.get("status"),
            "location": location(harness, sid),
            "machine_id": row.get("machine_id"),
            "machine": machine.get("name") or row.get("machine_id"),
            "remote": not here,
            "resume": None if mirrored else row.get("resume"),
            "mirrored": mirrored,
            # Remote control: whether the hub can write to it now
            # (attached) or could take it over (attachable). The chat
            # re-reads both from /v1/sessions when it opens.
            "ownership": row.get("ownership") or "observed",
            "attachable": bool(row.get("attachable")) and not mirrored,
            # Supervision detail for the chat chip and the details sheet:
            # the attach mode, the swe-… pane name, and whether an observed
            # row's pane was kept running.
            "mode": row.get("mode") or "",
            "tmux": row.get("tmux") or "",
            "kept": bool(row.get("kept")),
            # The model of the transcript's newest reply, as the hub's
            # catalog row carries it (remote/session_model.py); "" when
            # the hub doesn't know it, so a client shows nothing.
            "model": row.get("model") or "",
            # The hub's pin and name (remote/sessionmeta.py): "title" is
            # already the override when there is one, "harness_title" the
            # harness's own. "can_fork": the harness's CLI can fork it.
            "harness_title": row.get("harness_title") or "",
            "pinned": bool(row.get("pinned")),
            "pinned_at": row.get("pinned_at") if row.get("pinned") else None,
            "can_fork": bool(row.get("can_fork")) and not mirrored,
            "usage": u and {k: v for k, v in u.items() if k != "cwd"},
            "duration_s": (u["last_ts"] - u["first_ts"]) if u else None,
            "trivial": (not mirrored and is_trivial(harness, u, scanned,
                                   covered=ledger_covers(row, home, droid_ids))
                        if here else False),
        })
    out.sort(key=lambda r: (r["last_active_at"], r["key"] or ""), reverse=True)
    return out
