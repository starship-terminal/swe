"""``swe hooks stats`` and ``swe hooks log``: what the hooks did lately.

Both read the hook run log (``hooks/events.py``) and, unless
``--no-backfill`` is given, the Claude Code transcripts
(``hooks/transcripts.py``), merged so a run is counted once, each run
given to the ``swe hooks list`` row that owns it (``hooks/identity.py``).
They only read: the log is written by the hook shim, never here.
"""

from __future__ import annotations

import time
from pathlib import Path

from swe.console import c, sanitize
from swe.flags import expand_value_flags
from swe.hooks import events as _events
from swe.hooks import identity as _identity
from swe.hooks import transcripts as _transcripts
from swe.init.layout import aliases_of, load_registry
from swe.json_output import emit
from swe.paths import home_relative
from swe.table import Table

DEFAULT_SINCE = "7d"
DEFAULT_LIMIT = 20
# How far before the window to read, so a Stop retry can see its block.
LOOKBACK = 3600.0

_DECISION_COLOR = {"block": "red", "warn": "yellow", "error": "yellow",
                   "allow": "green"}

_VALUE_FLAGS = {"--since", "--hook", "--harness", "--repo", "--decision",
                "--limit", "--source"}


def _parse(rest: list[str], allowed: set[str], usage: str):
    """({flag: value}, backfill, positionals) or raises ValueError."""
    backfill = "--no-backfill" not in rest
    rest = [a for a in rest if a != "--no-backfill"]
    rest = expand_value_flags(rest, _VALUE_FLAGS)
    values: dict[str, str] = {}
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in _VALUE_FLAGS:
            if arg not in allowed:
                raise ValueError(f"Unknown option for this command: {arg}")
            values[arg] = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
        elif arg.startswith("-"):
            raise ValueError(f"Unknown option: {arg}. {usage}")
        else:
            positionals.append(arg)
            i += 1
    return values, backfill, positionals


def _resolve_harness(name: str, registry: dict, seen: set[str]) -> str:
    """The harness name events carry for ``--harness=<name>``; aliases too."""
    if name in seen or name in registry:
        return name
    matches = [key for key, entry in registry.items()
               if isinstance(entry, dict) and name in aliases_of(entry)]
    return matches[0] if len(matches) == 1 else name


def collect(home: Path, since: float | None, backfill: bool,
            now: float | None = None) -> dict:
    """Every event since ``since``: the log plus the transcripts, merged,
    each carrying the inventory row it belongs to (``identity.Owners``).

    Returns ``{"events": [...], "sources": {...}}``; ``sources`` counts
    the runs each source supplied after the merge and how many transcript
    runs the log already held.
    """
    # A retry that lands just inside the window may follow a block that
    # fell just outside it, so read a little further back, decide what each
    # run was, and cut to the window afterwards.
    context = None if since is None else since - LOOKBACK
    # One resolver for the read: the transcripts' commands and the
    # inventory's declared commands resolve the same way, once each.
    resolver = _identity.Resolver(home)
    helper = _events.read_log(home, context)
    transcript = (_transcripts.backfill(home, context, resolver)
                  if backfill else [])
    owners = _identity.Owners.load(home, resolver)
    merged, _ = _events.merge_events(helper, transcript,
                                     canonical=owners.canonical)
    owners.assign(merged)
    _events.assign_identities(merged)
    _events.mark_recovered(merged)
    if since is not None:
        merged = [e for e in merged if e["ts"] >= since]
        helper = [e for e in helper if e["ts"] >= since]
        transcript = [e for e in transcript if e["ts"] >= since]
    from_helper = sum(1 for e in merged if e["source"] == "helper")
    return {
        "events": merged,
        "sources": {
            "helper": from_helper,
            "transcript": len(merged) - from_helper,
            "duplicates": len(helper) + len(transcript) - len(merged),
            "backfill": backfill,
        },
    }


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "-"
    secs = max(0.0, now - ts)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "now"


def _pct(rate: float) -> str:
    return f"{rate * 100:.0f}%" if rate >= 0.1 or rate == 0 else \
        f"{rate * 100:.1f}%"


_event_json = _events.event_json


def _filtered(home: Path, values: dict, backfill: bool, now: float,
              hook: str | None) -> tuple[dict, list[dict], float | None]:
    since = _events.parse_since(values.get("--since", DEFAULT_SINCE), now)
    data = collect(home, since, backfill, now)
    registry = load_registry(home)
    harness = values.get("--harness")
    if harness:
        seen = {e["harness"] for e in data["events"]}
        harness = _resolve_harness(harness, registry, seen)
    selected = _events.filter_events(
        data["events"], hook=hook, harness=harness,
        repo=values.get("--repo"), decision=values.get("--decision"),
        since=since, source=values.get("--source"))
    return data, selected, since


# --- stats ------------------------------------------------------------------


def stats(rest: list[str], json_view: bool, fail) -> int:
    usage = "Usage: swe hooks stats [--since=7d] [--hook=<name>] " \
            "[--source=<path>] [--harness=<h>] [--repo=<path>] " \
            "[--no-backfill] [--sum-harnesses] [--json]"
    # One row per hook with every harness folded in before the per-row
    # reason cap, for a reader that wants exact totals (the Mac app and
    # the web page, which join rows to the list by ``source_paths``).
    sum_harnesses = "--sum-harnesses" in rest
    rest = [a for a in rest if a != "--sum-harnesses"]
    try:
        values, backfill, extra = _parse(
            rest, {"--since", "--hook", "--harness", "--repo", "--source"},
            usage)
    except ValueError as exc:
        return fail(c("red", str(exc)))
    if extra:
        return fail(c("red", f"Unexpected args: {' '.join(extra)}. {usage}"))
    home = Path.home()
    now = time.time()
    try:
        data, selected, since = _filtered(
            home, values, backfill, now, values.get("--hook"))
    except ValueError as exc:
        return fail(c("red", str(exc)))
    result = _events.build_stats(selected, sum_harnesses=sum_harnesses)
    window = values.get("--since", DEFAULT_SINCE)

    if json_view:
        return emit({
            "window": window,
            "since": since,
            "until": now,
            "filters": {
                "hook": values.get("--hook"),
                "harness": values.get("--harness"),
                "repo": values.get("--repo"),
                "source": values.get("--source"),
            },
            "sources": data["sources"],
            "totals": result["totals"],
            "hooks": result["hooks"],
            "uncertain": result["uncertain"],
        })

    totals, rows = result["totals"], result["hooks"]
    span = "all time" if since is None else f"last {window}"
    print(f"\n{c('bold', 'Hook stats')}  {c('dim', span)}\n")
    if not rows:
        print(c("dim", "  No hook runs recorded in this window."))
        print(c("dim", "  Shared hooks log themselves once `swe hooks sync` "
                       "has refreshed the shim;"))
        print(c("dim", "  Claude Code history is read from its transcripts "
                       "unless --no-backfill.\n"))
        return 0

    table = Table()
    table.add_column("hook", "HOOK", width=10, max_width=30)
    table.add_column("harness", "HARNESS", width=7, max_width=12)
    table.add_column("fires", "FIRES", width=5, kind="number")
    table.add_column("blocks", "BLOCKS", width=6, kind="number")
    table.add_column("rate", "RATE", width=5, kind="preformatted",
                     attrs={"right": True})
    table.add_column("warns", "WARNS", width=5, kind="number")
    table.add_column("errors", "ERRORS", width=6, kind="number")
    table.add_column("recovered", "RECOVERED", width=9, kind="number")
    table.add_column("last", "LAST BLOCK", width=10, kind="preformatted")
    for r in rows:
        rate = _pct(r["block_rate"])
        table.add_row({
            "hook": sanitize(r["hook"]),
            "harness": sanitize(r["harness"]) or
                       ("all" if sum_harnesses else "-"),
            "fires": r["fires"],
            "blocks": r["blocks"],
            "rate": c("red", rate) if r["blocks"] else c("dim", rate),
            "warns": r["warns"],
            "errors": r["errors"],
            "recovered": r["recovered"],
            "last": c("dim", _ago(r["last_block"], now)),
        })
    for line in table.render():
        print(f"  {line}")

    reasons = [(r, t) for r in rows for t in r["top_reasons"]]
    if reasons:
        print(f"\n  {c('bold', 'Top reasons')}\n")
        rtable = Table()
        rtable.add_column("hook", "HOOK", width=10, max_width=30)
        rtable.add_column("count", "COUNT", width=5, kind="number")
        rtable.add_column("decision", "DECISION", width=8,
                          kind="preformatted")
        rtable.add_column("reason", "REASON", width=20, max_width=90)
        for r, t in reasons[:20]:
            rtable.add_row({
                "hook": sanitize(r["hook"]),
                "count": t["count"],
                "decision": c(_DECISION_COLOR.get(t["decision"], "dim"),
                              t["decision"]),
                "reason": sanitize(t["reason"]),
            })
        for line in rtable.render():
            print(f"  {line}")

    src = data["sources"]
    where = (f"{src['helper']} from the hook log, {src['transcript']} from "
             f"transcripts" if src["backfill"]
             else f"{src['helper']} from the hook log")
    summary = (f"{totals['fires']} runs · {totals['blocks']} blocks "
               f"({_pct(totals['block_rate'])}) · {where}")
    print(f"\n  {c('dim', summary)}\n")
    return 0


# --- log --------------------------------------------------------------------


def _shown_name(name: str | None, source: str | None,
                selected: list[dict]) -> str:
    """The name ``swe hooks log`` heads its answer with: the one asked for
    as the events carry it, else the owning row's list name."""
    if name:
        wanted = _events.resolve_hook_name(
            name, {e["hook"] for e in selected}
            | set(_events.group_names(selected).values()))
        return wanted if selected else _events.strip_script_extension(name)
    for event in selected:
        if event.get("_owner_name"):
            return event["_owner_name"]
    return _events.strip_script_extension(Path(source or "").name)


def log(rest: list[str], json_view: bool, fail) -> int:
    usage = "Usage: swe hooks log <name> | --source=<path> [--since=7d] " \
            "[--harness=<h>] [--repo=<path>] [--decision=<d>] [--limit=20] " \
            "[--no-backfill] [--json]"
    try:
        values, backfill, names = _parse(
            rest, {"--since", "--harness", "--repo", "--decision", "--limit",
                   "--source"},
            usage)
    except ValueError as exc:
        return fail(c("red", str(exc)))
    source = values.get("--source")
    if len(names) > 1 or (not names and not source):
        return fail(c("red", usage))
    decision = values.get("--decision")
    if decision and decision not in _events.DECISIONS:
        return fail(c("red", f"--decision={decision}: use one of "
                             f"{', '.join(_events.DECISIONS)}"))
    try:
        limit = int(values.get("--limit", DEFAULT_LIMIT))
        if limit < 1:
            raise ValueError
    except ValueError:
        return fail(c("red", f"--limit={values.get('--limit')}: "
                             "write a positive number"))
    home = Path.home()
    now = time.time()
    try:
        data, selected, since = _filtered(home, values, backfill, now,
                                          names[0] if names else None)
    except ValueError as exc:
        return fail(c("red", str(exc)))
    recent = sorted(selected, key=lambda e: e["ts"], reverse=True)[:limit]
    shown = _shown_name(names[0] if names else None, source, selected)

    if json_view:
        return emit({
            "hook": shown,
            "source_path": source,
            "window": values.get("--since", DEFAULT_SINCE),
            "since": since,
            "matched": len(selected),
            "events": [_event_json(e) for e in recent],
        })

    counts = f"{len(recent)} of {len(selected)} runs, newest first"
    print(f"\n{c('bold', sanitize(shown))}  "
          f"{c('dim', counts)}\n")
    if not recent:
        print(c("dim", "  No runs of that hook in this window.\n"))
        return 0
    table = Table()
    table.add_column("when", "WHEN", width=8, kind="preformatted")
    table.add_column("decision", "DECISION", width=8, kind="preformatted")
    table.add_column("event", "EVENT", width=8, max_width=24)
    table.add_column("harness", "HARNESS", width=7, max_width=12)
    table.add_column("ms", "MS", width=5, kind="number")
    table.add_column("repo", "REPO", width=10, max_width=28, kind="path")
    table.add_column("branch", "BRANCH", width=6, max_width=22)
    table.add_column("reason", "REASON", width=10, max_width=70)
    for e in recent:
        place = e["repo"] or e["cwd"] or ""
        table.add_row({
            "when": c("dim", _ago(e["ts"], now)),
            "decision": c(_DECISION_COLOR.get(e["decision"], "dim"),
                          e["decision"]),
            "event": sanitize(e["event"]) or "-",
            "harness": sanitize(e["harness"]) or "-",
            "ms": e["duration_ms"],
            "repo": home_relative(Path(place), home) if place else "-",
            "branch": sanitize(e["git_branch"] or "") or "-",
            "reason": sanitize(e["reason"]),
        })
    for line in table.render():
        print(f"  {line}")
    print()
    return 0
