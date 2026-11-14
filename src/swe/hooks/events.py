"""The hook run log: reading it, merging it with transcripts, counting it.

``shim_runtime.record`` appends one JSON line per hook run to
``~/.swe/cache/hooks/events.jsonl`` (and rotates it to ``events.jsonl.1``).
``hooks/transcripts.py`` recovers the same kind of event from Claude Code
transcripts, for history from before the helper existed. This module reads
the log, merges the two sources without counting a run twice, filters, and
folds events into the per-hook numbers ``swe hooks stats`` prints.

An event is a plain dict::

    ts, hook, harness, event, session_id, cwd, repo, git_branch,
    decision (block|warn|allow|error), reason, duration_ms, exit_code,
    stop_hook_active (bool or None), source (helper|transcript),
    script (the resolved file that ran, or None)

Once events are merged, ``hooks/identity.py`` adds ``_owner`` (the
``swe hooks list`` row the run belongs to, as its resolved source),
``_owner_name``, ``_sources`` and ``_maybe``, and ``assign_identities``
adds ``_identity``; the leading underscore keeps them out of every
``--json`` shape (``event_json`` writes ``source_paths`` from
``_sources``).

Nothing here writes: the transcript events live in memory for one
command and are never added to the log.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from swe import paths as _paths
from swe.hooks import shim_runtime

DECISIONS = ("block", "warn", "allow", "error")
TOP_REASONS = 5
# A transcript event and a helper event for the same run are one run when
# their timestamps are this close (seconds). See merge_events.
DEDUPE_WINDOW = 5.0

# Script extensions dropped from a hook's name, so ``prose-guard.py`` and
# the ``prose-guard`` the transcripts name are the same hook.
SCRIPT_EXTENSIONS = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs",
                      ".ts", ".rb", ".pl")

_SINCE_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def hook_stem(name: str) -> str:
    """``prose-guard`` for ``prose-guard.py`` or ``/x/hooks/prose-guard.py``."""
    base = os.path.basename(str(name or "").strip())
    lowered = base.lower()
    for ext in SCRIPT_EXTENSIONS:
        if lowered.endswith(ext) and len(base) > len(ext):
            return base[:-len(ext)]
    return base


def strip_script_extension(name: str) -> str:
    """``claude-plugin/on-stop`` for ``claude-plugin/on-stop.sh``: the
    extension goes, a folder prefix stays (the stats name of a hook another
    installer declared carries one)."""
    name = str(name or "").strip()
    lowered = name.lower()
    for ext in SCRIPT_EXTENSIONS:
        if lowered.endswith(ext) and len(name) > len(ext):
            return name[:-len(ext)]
    return name


def resolve_hook_name(requested: str, names) -> str:
    """The hook name ``requested`` means, among the ``names`` events carry.

    A name exactly as ``swe hooks stats`` prints it wins (so
    ``claude-plugin/on-stop`` finds that hook, not the bare ``on-stop``);
    then the name without its script extension; then the bare script stem,
    which also takes ``/x/hooks/prose-guard.py`` and a name nothing ran
    under yet.
    """
    names = set(names)
    exact = str(requested or "").strip()
    for candidate in (exact, strip_script_extension(exact)):
        if candidate in names:
            return candidate
    return hook_stem(exact)


def parse_since(text: str, now: float) -> float | None:
    """The epoch second ``--since=<n><s|m|h|d|w>`` starts at; None for ``all``.

    Raises ValueError with the message the command prints.
    """
    value = (text or "").strip().lower()
    if value == "all":
        return None
    match = _SINCE_RE.match(value)
    if not match or int(match.group(1)) <= 0:
        raise ValueError(
            f"--since={text}: write a span like 7d, 24h, 90m or 2w, or all")
    return now - int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


# --- reading the log --------------------------------------------------------


def _clean(raw) -> dict | None:
    """One log line as an event dict, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    ts, hook = raw.get("ts"), raw.get("hook")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not isinstance(hook, str) or not hook:
        return None
    decision = raw.get("decision")
    if decision not in DECISIONS:
        return None

    def text(key):
        value = raw.get(key)
        return value if isinstance(value, str) else None

    duration = raw.get("duration_ms")
    exit_code = raw.get("exit_code")
    active = raw.get("stop_hook_active")
    return {
        "ts": float(ts),
        "hook": hook_stem(hook),
        "harness": text("harness") or "",
        "event": text("event") or "",
        "session_id": text("session_id"),
        "cwd": text("cwd"),
        "repo": text("repo"),
        "git_branch": text("git_branch"),
        "decision": decision,
        "reason": (text("reason") or "")[:shim_runtime.REASON_MAX],
        "duration_ms": (int(duration) if isinstance(duration, (int, float))
                        and not isinstance(duration, bool) else None),
        "exit_code": (exit_code if isinstance(exit_code, int)
                      and not isinstance(exit_code, bool) else None),
        "stop_hook_active": active if isinstance(active, bool) else None,
        "source": "helper",
        # Lines from before the field have none: they are matched by name.
        "script": (text("script") if (text("script") or "").startswith("/")
                   else None),
    }


def read_log(home: Path, since: float | None = None) -> list[dict]:
    """Every event in the log and its rotated generation, oldest first.

    A missing file, an unreadable one and a half-written or foreign line
    each read as nothing: the log is best-effort by design.
    """
    live = _paths.hook_events_file_for(home)
    out: list[dict] = []
    for path in (live.with_name(live.name + ".1"), live):
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = _clean(json.loads(line))
                except ValueError:
                    continue
                if event and (since is None or event["ts"] >= since):
                    out.append(event)
    out.sort(key=lambda e: e["ts"])
    return out


# --- merging helper and transcript events -----------------------------------


def _base_event(name: str) -> str:
    """``PreToolUse`` for ``PreToolUse:Bash``."""
    return (name or "").split(":", 1)[0]


def merge_events(helper: list[dict], transcript: list[dict],
                 window: float = DEDUPE_WINDOW,
                 canonical=None) -> tuple[list[dict], int]:
    """(events, duplicates dropped): each run counted once, helper first.

    The same run can sit in both sources. A transcript event is that run
    when a helper event has the same ``session_id``, the same event name
    before any ``:``, a timestamp within ``window`` seconds, and the same
    script. Scripts decide whenever both events know theirs (two scripts
    with one name are two hooks, and one script under two names is one);
    only when one side has none (a log line from before ``script``, a
    command with a variable swe cannot resolve) do names decide: the same
    name, or a log line's name that is the transcript script's stem (what
    the shim and ``record`` log). Exact matches are made first, so a name
    match never takes a copy a script match wanted.

    Matching is one to one and in time order, so two real fires a second
    apart in one session still count twice. The helper's event wins, since
    it carries the exact reason and ``stop_hook_active``, and takes the
    transcript's script when it had none.

    ``canonical`` (``identity.Owners.canonical``) maps a script to the one
    file it stands for before comparing, so a transcript that reached a
    shared script through a wrapper swe could not read still meets the
    shim's copy of the run.
    """
    if canonical is not None:
        helper = [dict(e, script=canonical(e.get("script"))) for e in helper]
        transcript = [dict(e, script=canonical(e.get("script")))
                      for e in transcript]
    else:
        helper = [dict(e) for e in helper]
    used_h: set[int] = set()
    used_t: set[int] = set()
    for helper_key, transcript_key in _MERGE_PASSES:
        pools: dict[tuple, list[int]] = {}
        for i, event in enumerate(helper):
            key = helper_key(event)
            if i not in used_h and key is not None:
                pools.setdefault(_run_key(event, key), []).append(i)
        groups: dict[tuple, list[int]] = {}
        for j, event in enumerate(transcript):
            key = transcript_key(event)
            if j not in used_t and key is not None:
                groups.setdefault(_run_key(event, key), []).append(j)
        for run, group in groups.items():
            pool = sorted(pools.get(run, []), key=lambda i: helper[i]["ts"])
            free = 0  # first helper event not yet passed or used
            for j in sorted(group, key=lambda j: transcript[j]["ts"]):
                ts = transcript[j]["ts"]
                # Both lists are time sorted, so a helper event too old for
                # this transcript event is too old for every later one: an
                # ordered greedy pass finds the most matches the window
                # allows, where taking the nearest copy first can strand
                # one.
                while free < len(pool) and helper[pool[free]]["ts"] < ts - window:
                    free += 1
                if free < len(pool) and helper[pool[free]]["ts"] <= ts + window:
                    i = pool[free]
                    free += 1
                    used_h.add(i)
                    used_t.add(j)
                    if not helper[i].get("script") and transcript[j].get("script"):
                        helper[i]["script"] = transcript[j]["script"]
    kept = helper + [e for j, e in enumerate(transcript) if j not in used_t]
    kept.sort(key=lambda e: e["ts"])
    return kept, len(used_t)


def _scripted(event: dict) -> str | None:
    return event.get("script") or None


def _name_if_unscripted(event: dict) -> str | None:
    return None if event.get("script") else event["hook"]


def _name_if_scripted(event: dict) -> str | None:
    return event["hook"] if event.get("script") else None


def _script_stem(event: dict) -> str | None:
    return hook_stem(event["script"]) if event.get("script") else None


# (helper key, transcript key) per pass, strictest first. A None key leaves
# the event out of that pass; two scripted events with different scripts
# never meet in any of them.
_MERGE_PASSES = (
    (_scripted, _scripted),
    (_name_if_unscripted, lambda e: e["hook"]),
    (_name_if_scripted, _name_if_unscripted),
    (_name_if_unscripted, _script_stem),
)


def _run_key(event: dict, identity) -> tuple:
    return (event["session_id"], identity, _base_event(event["event"]))


def identity_key(event: dict) -> tuple:
    """What makes two events the same hook: the inventory row that owns
    them, else the script that ran, else the name (``assign_identities``
    settles the last for a whole read)."""
    if event.get("_identity"):
        return event["_identity"]
    if event.get("_owner"):
        return ("owner", event["_owner"])
    if event.get("script"):
        return ("script", event["script"])
    return ("name", event["hook"])


def script_key(event: dict) -> tuple:
    """The one script a run was, finer than ``identity_key`` inside a
    directory hook: its executables recover apart, though they count in
    one row. A run with no script of its own, or the file a file row is,
    falls back to the row's identity."""
    script = event.get("script")
    if script and script != event.get("_owner"):
        return ("script", script)
    return identity_key(event)


def group_names(events: list[dict]) -> dict[tuple, str]:
    """The name ``build_stats`` prints for each hook in ``events``, by
    ``identity_key`` and by (identity, harness)."""
    groups: dict[tuple, list[dict]] = {}
    for event in events:
        key = identity_key(event)
        groups.setdefault(key, []).append(event)
        groups.setdefault((key, event["harness"]), []).append(event)
    return {key: _row_name(group) for key, group in groups.items()}


def assign_identities(events: list[dict]) -> None:
    """Set ``_identity`` on every event (see ``identity_key``).

    A log line no listed script owns and that knows no script of its own
    (written before ``script``) joins the one unowned script whose
    runs carry its name, or whose stem it is, so a hook's old and new runs
    read as one row; with two such scripts it stays under its name, never
    in either.
    """
    by_name: dict[str, set[str]] = {}
    for event in events:
        if not event.get("_owner") and event.get("script"):
            for name in (event["hook"], hook_stem(event["script"])):
                by_name.setdefault(name, set()).add(event["script"])
    for event in events:
        event.pop("_identity", None)
        key = identity_key(event)
        # Only a log line: a transcript run with no script ran a command
        # swe cannot resolve, which may well be another file of that name.
        if key[0] == "name" and event["source"] == "helper":
            scripts = by_name.get(event["hook"], set())
            if len(scripts) == 1:
                key = ("script", next(iter(scripts)))
        event["_identity"] = key


def mark_recovered(events: list[dict]) -> None:
    """Set ``recovered`` on every event and fill ``stop_hook_active``.

    ``recovered`` is "blocked, then the rewrite was accepted": a Stop run
    that allowed, right after the same hook blocked in the same session.
    ``stop_hook_active`` alone cannot say it, because the harness sets it
    when any Stop hook blocked, so it must agree with that hook's own
    previous run. A Stop event with no flag (every transcript event) gets
    it from the same rule. Events outside the Stop family are never
    recovered.
    """
    previous: dict[tuple, str] = {}
    for event in sorted(events, key=lambda e: e["ts"]):
        event["recovered"] = False
        if _base_event(event["event"]) not in ("Stop", "SubagentStop"):
            continue
        key = (event["session_id"], script_key(event), event["event"])
        after_block = previous.get(key) == "block"
        if event["stop_hook_active"] is None:
            event["stop_hook_active"] = after_block
        event["recovered"] = (event["decision"] == "allow" and after_block
                              and event["stop_hook_active"] is True)
        previous[key] = event["decision"]


# --- filters ----------------------------------------------------------------

_REAL: dict[str, str] = {}


def _real(path: str) -> str:
    cached = _REAL.get(path)
    if cached is None:
        try:
            cached = os.path.realpath(path)
        except (OSError, ValueError):
            cached = path
        _REAL[path] = cached
    return cached


def _within(path: str | None, root: str) -> bool:
    if not path:
        return False
    real = _real(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def repo_filter(path: str):
    """A predicate for ``--repo=<path>``: the event ran in that repository.

    ``path`` may be the repo root or any folder inside it. An event with a
    recorded repo matches when that is this repo's root: a nested
    repository or submodule below ``path`` is another project. Only an
    event with no recorded repo (a transcript whose folder is gone) falls
    back to its cwd sitting under ``path``.
    """
    root = _real(os.path.expanduser(path))
    top, _ = shim_runtime.repo_info(root)
    top = _real(top) if top else root

    def match(event: dict) -> bool:
        repo = event.get("repo")
        if repo:
            return _real(repo) == top
        return _within(event.get("cwd"), root)

    return match


def source_filter(path: str):
    """A predicate for ``--source=<path>``: the run belongs to the
    ``swe hooks list`` row whose ``source_path`` is ``path`` (any spelling
    of the same file)."""
    wanted = _real(os.path.expanduser(path))

    def match(event: dict) -> bool:
        return bool(event.get("_owner")) and _real(event["_owner"]) == wanted

    return match


def filter_events(events: list[dict], *, hook: str | None = None,
                  harness: str | None = None, repo: str | None = None,
                  decision: str | None = None,
                  since: float | None = None,
                  source: str | None = None) -> list[dict]:
    """The events matching every given filter, order kept.

    ``hook`` takes a name the events were logged under or a name
    ``swe hooks stats`` prints for a row (``bundle`` for its executables'
    runs, an unowned script's most common name for every name it ran
    under), and then finds every run that row counts.
    """
    names = group_names(events) if hook else {}
    wanted = resolve_hook_name(
        hook, {e["hook"] for e in events} | set(names.values())) \
        if hook else None
    in_repo = repo_filter(repo) if repo else None
    owned = source_filter(source) if source else None
    out = []
    for event in events:
        if since is not None and event["ts"] < since:
            continue
        if wanted and event["hook"] != wanted \
                and names.get(identity_key(event)) != wanted \
                and names.get((identity_key(event), event["harness"])) != wanted:
            continue
        if harness and event["harness"] != harness:
            continue
        if decision and event["decision"] != decision:
            continue
        if in_repo and not in_repo(event):
            continue
        if owned and not owned(event):
            continue
        out.append(event)
    return out


# --- counting ---------------------------------------------------------------


def _rate(blocks: int, fires: int) -> float:
    return round(blocks / fires, 4) if fires else 0.0


def _fold(events: list[dict]) -> dict:
    counts = Counter(e["decision"] for e in events)
    fires = len(events)
    durations = [e["duration_ms"] for e in events
                 if e["duration_ms"] is not None]
    blocks = [e["ts"] for e in events if e["decision"] == "block"]
    reasons: Counter = Counter(
        (e["decision"], e["reason"]) for e in events
        if e["decision"] != "allow" and e["reason"])
    ranked = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "fires": fires,
        "blocks": counts["block"],
        "warns": counts["warn"],
        "errors": counts["error"],
        "allows": counts["allow"],
        # Stop runs that came right after a block of the same hook and
        # let the turn end: "blocked, then the rewrite was accepted".
        "recovered": sum(1 for e in events if e.get("recovered")),
        "block_rate": _rate(counts["block"], fires),
        "last_fired": max((e["ts"] for e in events), default=None),
        "last_block": max(blocks, default=None),
        "avg_duration_ms": (round(sum(durations) / len(durations))
                            if durations else None),
        "top_reasons": [
            {"decision": decision, "reason": reason, "count": count}
            for (decision, reason), count in ranked[:TOP_REASONS]],
    }


EVENT_FIELDS = ("ts", "hook", "harness", "event", "session_id", "cwd",
                "repo", "git_branch", "decision", "reason", "duration_ms",
                "exit_code", "stop_hook_active", "source", "script")


def event_json(event: dict) -> dict:
    """One event as ``swe hooks log --json`` shows it."""
    out = {k: event.get(k) for k in EVENT_FIELDS}
    out["source_paths"] = list(event.get("_sources") or [])
    return out


def _row_name(group: list[dict]) -> str:
    """An owned row is named as the list names it; any other row by the
    name most of its runs carry (the helper's, on a tie)."""
    for event in group:
        if event.get("_owner_name"):
            return event["_owner_name"]
    counts = Counter(e["hook"] for e in group)
    helper = {e["hook"] for e in group if e["source"] == "helper"}
    return min(counts, key=lambda name: (-counts[name], name not in helper,
                                         name))


def build_stats(events: list[dict], *, sum_harnesses: bool = False,
                recent: int = 0) -> dict:
    """``{"totals", "hooks", "uncertain"}`` for ``events``.

    One row per hook and harness, most blocks first, where a hook is the
    ``swe hooks list`` row that owns the runs (``source_paths``), else the
    script that ran, else the name (``identity_key``): two scripts that
    share a name are two rows, and one script logged under two names is
    one. The rows are the shape ``swe hooks stats --json`` documents.
    ``uncertain`` lists the ``source_path`` of every row a run with no
    script could belong to, its name being one more than one source
    answers to; its counts may be short by those runs.

    ``sum_harnesses`` folds every harness into one row per hook before the
    five-reason cap (``harness`` is then ``""`` and ``harnesses`` lists
    the ones that ran it). ``recent`` adds that many newest runs to each
    row (``event_json``), for the web page.
    """
    groups: dict[tuple, list[dict]] = {}
    for event in events:
        harness = "" if sum_harnesses else event["harness"]
        groups.setdefault((identity_key(event), harness), []).append(event)
    rows = []
    for (_identity, harness), group in groups.items():
        row = {
            "hook": _row_name(group),
            "harness": harness,
            "source_paths": list(group[0].get("_sources") or []),
            "scripts": sorted({e["script"] for e in group if e.get("script")}),
        }
        if sum_harnesses:
            row["harnesses"] = sorted({e["harness"] for e in group
                                       if e["harness"]})
        row.update(_fold(group))
        if recent:
            newest = sorted(group, key=lambda e: e["ts"], reverse=True)
            row["recent"] = [event_json(e) for e in newest[:recent]]
        rows.append(row)
    rows.sort(key=lambda r: (-r["blocks"], -r["fires"], r["hook"],
                             r["harness"], r["source_paths"], r["scripts"]))
    totals = _fold(events)
    totals.pop("top_reasons")
    totals["hooks"] = len(rows)
    uncertain = sorted({s for e in events for s in e.get("_maybe") or ()})
    return {"totals": totals, "hooks": rows, "uncertain": uncertain}
