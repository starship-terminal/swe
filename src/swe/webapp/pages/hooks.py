"""The Hooks page: ``swe hooks list``/``info``/``check`` built in process,
plus ``swe init`` for linking and ``swe hooks new`` for a skeleton.

``GET /v1/app/hooks`` is the Mac app's HooksPage list: every script under
``~/.swe/hooks`` (harness, shared and foreign tiers) with its link
state and declarations, the ``swe hooks check`` findings, the paths a
plain ``swe init`` would leave a real file over, and the harness names
the New hook sheet offers. A ``.linkignore`` that cannot be read sets
``warning`` and the list is built without it, the way ``swe hooks``
reports the same thing; a check that fails sets ``check_error`` and the
list still shows, the Mac app's own rule.

``GET /v1/app/hooks?hook=<name>[&harness=<h>][&tier=<t>]`` is the detail: one row,
found the way ``swe hooks info`` finds it (exact filename, else stem;
``harness`` scopes it through the registry's aliases), plus the docstring
summary, whether the source exists and sits inside ``~/.swe/hooks``
(the Mac app's edit rule — only those scripts may be edited, and the web
app edits nothing), the declarations in full, and the script's text when
it previews.

``POST /v1/app/hooks/link`` runs ``swe init`` and ``POST
/v1/app/hooks/replace`` runs ``swe init --force``, in process through the
Doctor page's ``run_init_now`` — the AGENTS.md page's pattern — so every
page shares the one-fix lock (409 ``busy``). ``POST /v1/app/hooks/new``
``{"harness", "name"}`` is ``swe hooks new``: the skeleton only, never a
settings file, so the reply carries the stanza to paste. ``harness`` of
``shared`` is ``swe hooks new shared``; anything else resolves through
the registry's aliases first. Everything else is a 404.

``GET /v1/app/hooks?stats=<24h|7d|30d>`` is what the hooks did in that
window: ``swe hooks stats --json`` and ``swe hooks log --json`` read in
process (``hooks/stats_commands.py::collect``), one row per hook script
summed across harnesses, each with its top reasons and its five newest
runs. The read can take seconds on a large transcript library (it
backfills from Claude Code's transcripts), so no request runs it: a
background thread does, its answer is cached for ``STATS_TTL_S``, and a
request that finds nothing ready answers ``{"state": "loading"}`` after
at most ``FIRST_WAIT_S`` for the client to ask again. The list and detail
reads above never touch it.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from swe.webapp.pages import PageError, home_path
from swe.webapp.pages.doctor import blocked_now
from swe.webapp.pages.skills import _inside, read_text

# ``swe hooks new`` writes one hub file and reads the registry; serialize
# it with every other write the page makes, as skills.py does.
_WRITE_LOCK = threading.Lock()


def get(ctx, args: dict) -> dict:
    home = home_path(ctx)
    if args.get("stats"):
        return stats_view(home, args["stats"], force=args.get("refresh") == "1")
    if args.get("hook"):
        return detail(ctx, home, args)
    return listing(ctx, home)


def post(ctx, action: str, body: dict) -> dict:
    if action == "new":
        if not isinstance(body, dict):
            raise PageError(422, "bad_body", "Send a JSON object.")
        return _new(ctx, home_path(ctx), body)
    force = {"link": False, "replace": True}.get(action)
    if force is None:
        raise PageError(404, "unknown_action", f"unknown hooks action: {action}")
    from swe.webapp.pages.doctor import run_init_now

    home = home_path(ctx)
    error = run_init_now(home, force=force)
    return {"ran": action, "error": error, **listing(ctx, home)}


# -- the list ------------------------------------------------------------


def _inventory(home: Path):
    """All rows, their registry and the .linkignore patterns (with the
    warning a bad one sets), mirroring ``commands._rows``."""
    from swe.hooks.inventory import all_rows, declarations_by_harness
    from swe.init.hooks import plan_hooks
    from swe.init.layout import (
        LinkIgnoreError,
        load_linkignore,
        load_registry,
    )

    registry = load_registry(home)
    warning = None
    try:
        patterns = load_linkignore(home)
    except LinkIgnoreError as exc:
        warning = str(exc) or type(exc).__name__
        patterns = []
    statuses = plan_hooks(home, patterns=patterns, registry=registry)
    declared = declarations_by_harness(home, registry)
    return all_rows(home, registry, statuses, declared), registry, patterns, warning


def _row_json(r, keys: list[str] | None = None) -> dict:
    """``commands._row_json`` plus the row's own ``detail`` note and the
    stats names its run counts come from."""
    return {
        "stats_keys": keys or [],
        "name": r.script,
        "harness": r.harness,
        "tier": r.tier,
        "source_path": str(r.source),
        "dest_path": str(r.dest),
        "link_status": r.state,
        "enabled": r.enabled,
        "detail": r.detail,
        "declared": [{
            "event": d.event,
            "matcher": d.matcher,
            "file": str(d.settings),
        } for d in r.declared],
        "events_supported": r.events_supported,
    }


def _harnesses(registry: dict, rows) -> list[str]:
    """The New hook sheet's options: every non-archived registry key plus
    the harness names the rows carry (a dormant shared row's is
    ``shared``)."""
    names = {key for key, entry in registry.items()
             if not isinstance(entry, dict) or entry.get("state") != "archived"}
    names |= {r.harness for r in rows}
    return sorted(names)


def listing(ctx, home: Path) -> dict:
    """The list payload, ``GET /v1/app/hooks`` with no arguments."""
    rows, registry, patterns, warning = _inventory(home)
    try:
        from swe.hooks.inventory import hook_findings

        findings = [{"kind": f.kind, "harness": f.harness, "message": f.message}
                    for f in hook_findings(home, registry=registry, patterns=patterns)]
        check_error = None
    except Exception as exc:  # noqa: BLE001 - the Mac app's checkError
        findings, check_error = [], str(exc) or type(exc).__name__
    return {
        "machine": ctx.machine.get("name"),
        "home": str(home),
        "hooks": [_row_json(r, k) for r, k in zip(rows, _stats_keys(rows, home))],
        "findings": findings,
        "check_error": check_error,
        "warning": warning,
        "blocked": blocked_now(home),
        "harnesses": _harnesses(registry, rows),
    }


# -- which stats a row owns ------------------------------------------------

_TOKEN_BREAKS = re.compile(r"[\s\"'`;|&()<>=]+")


def _expand(token: str, home: Path) -> str:
    for var in ("$HOME", "${HOME}"):
        token = token.replace(var, str(home))
    return str(home) + token[1:] if token == "~" or token.startswith("~/") else token


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _stats_keys(rows, home: Path) -> list[list[str]]:
    """For each row, the names its runs may be logged under, never one
    another row's.

    The log names a run by its script (``events.hook_stem``; a generic
    stem takes its folder, ``transcripts.command_hook``), so a row cannot
    just use its list name: a directory hook is named after its folder
    while its runs carry the executable's stem, and two scripts with one
    basename share a stem. A row's names are its own file's stem and
    ``command_hook`` of its path, plus ``command_hook`` of every declared
    command that points at it (inside it, for a directory) or, when the
    command's path holds an unresolved variable, that has its basename. A
    name that more than one distinct source file could own is dropped.
    """
    from swe.hooks.events import SCRIPT_EXTENSIONS, hook_stem
    from swe.hooks.transcripts import command_hook

    out: list[list[str]] = []
    owners: dict[str, set[str]] = {}
    for r in rows:
        src = Path(r.source)
        is_dir = src.is_dir()
        real_src, real_dest = _real(str(r.source)), _real(str(r.dest))
        keys: list[str] = []

        def add(key: str) -> None:
            if key and key not in keys:
                keys.append(key)

        if not is_dir:
            add(hook_stem(src.name))
            for path in (str(src), str(r.dest)):
                add(command_hook(path))
        for decl in getattr(r, "declared", []):
            for token in _TOKEN_BREAKS.split(decl.command or ""):
                scripted = token.lower().endswith(SCRIPT_EXTENSIONS) and len(token) > 3
                # A directory's executable may have no extension at all.
                if not scripted and not (is_dir and "/" in token):
                    continue
                path = _expand(token, home)
                if "$" in path:
                    # A plugin root the log resolved and the row cannot:
                    # the basename is all there is to go on.
                    mine = not is_dir and hook_stem(os.path.basename(path)) == hook_stem(src.name)
                else:
                    real = _real(path)
                    roots = (real_src, real_dest)
                    mine = (any(real.startswith(root.rstrip(os.sep) + os.sep) for root in roots)
                            if is_dir else real in roots)
                if mine:
                    add(command_hook(token))
        for key in keys:
            owners.setdefault(key, set()).add(real_src)
        out.append(keys)
    return [[k for k in keys if len(owners[k]) == 1] for keys in out]


# -- one hook -------------------------------------------------------------


def detail(ctx, home: Path, args: dict) -> dict:
    """``GET ?hook=<name>[&harness=<h>]``: the row plus the script itself."""
    from swe import paths as _paths
    from swe.hooks.commands import _resolve_harness
    from swe.hooks.inventory import find_script, script_summary

    rows, registry, _patterns, _warning = _inventory(home)
    matches = find_script(args["hook"], rows)
    if args.get("harness"):
        wanted = _resolve_harness(args["harness"], registry, {r.harness for r in rows})
        matches = [r for r in matches if r.harness == wanted]
    # A shared script and a per-harness one can share name and harness:
    # only the tier tells them apart.
    if args.get("tier"):
        matches = [r for r in matches if r.tier == args["tier"]]
    if not matches:
        raise PageError(404, "unknown_hook",
                        f"No hook script named {args['hook']} under ~/.swe/hooks.")
    row = matches[0]
    keys = {id(r): k for r, k in zip(rows, _stats_keys(rows, home))}[id(row)]
    source = Path(row.source)
    exists = source.exists()
    # A foreign hook's command can name any file on disk; a paired
    # device reads its contents (header or text) only under home.
    readable = _inside(source, home)
    return {
        "machine": ctx.machine.get("name"),
        "home": str(home),
        **_row_json(row, keys),
        "declared": [{
            "event": d.event,
            "matcher": d.matcher,
            "timeout": d.timeout,
            "command": d.command,
            "file": str(d.settings),
        } for d in row.declared],
        "summary": script_summary(source) if readable and source.is_file() else "",
        "exists": exists,
        "is_dir": source.is_dir(),
        # HookScript.editable: the source resolves inside the hooks root.
        "editable": exists and _inside(source, _paths.hooks_dir_for(home)),
        "text": read_text(source) if readable else None,
    }


# -- new -------------------------------------------------------------------


def _new(ctx, home: Path, body: dict) -> dict:
    from swe.hooks import create
    from swe.hooks.commands import _resolve_harness
    from swe.init.layout import load_registry

    harness, name = body.get("harness"), body.get("name")
    for key, value in (("harness", harness), ("name", name)):
        if not isinstance(value, str) or len(value) > 256:
            raise PageError(422, "bad_hook", f"{key} must be the new hook's {key}")
    with _WRITE_LOCK:
        registry = load_registry(home)
        try:
            if harness == "shared":
                created = _new_shared(name, home, registry)
            else:
                # plan_hooks treats the folder name as a registry key, so an
                # alias resolves first, as in commands._new.
                resolved = _resolve_harness(harness, registry, set(registry))
                if problem := create.validate_args(resolved, name):
                    raise ValueError(problem)
                created = _new_hook(resolved, name, home, registry)
        except ValueError as exc:
            raise PageError(422, "bad_hook", str(exc)) from exc
        reply = listing(ctx, home)
    return {"action": "new", "created": created, **reply}


def _new_hook(harness: str, name: str, home: Path, registry: dict) -> dict:
    """``commands._new``: the skeleton plus the stanza to paste."""
    from swe.hooks.contracts import contract_for
    from swe.hooks.create import (
        command_for,
        create,
        settings_stanza,
        settings_targets,
    )

    target = create(harness, name, home)
    command, _note = command_for(harness, target.name, registry, home)
    stanza = settings_stanza(command, contract_for(registry, harness, home))
    targets = settings_targets(registry, harness, home)
    return {
        "path": str(target),
        "harness": harness,
        "command": command,
        "settings": [str(t) for t in targets],
        "stanza": stanza,
    }


def _new_shared(name: str, home: Path, registry: dict) -> dict:
    """``commands._new_shared``: the normalized-contract skeleton and the
    first deployable harness's stanza, when one exists."""
    from swe.hooks import shared as _shared
    from swe.hooks.contracts import contract_for
    from swe.hooks.create import (
        create_shared,
        settings_stanza,
        shared_command_for,
    )

    target = create_shared(name, home)
    targets = [t for t in _shared.deploy_targets(home, registry) if t.source == target]
    commands = {t.harness: shared_command_for(t.harness, t.source.name, registry, home)
                for t in targets}
    stanza = None
    for harness_name in sorted(commands):
        command = commands[harness_name]
        if command:
            stanza = settings_stanza(command, contract_for(registry, harness_name, home))
            break
    return {
        "path": str(target),
        "harness": "shared",
        "tier": "shared",
        "commands": commands,
        "deploys_to": sorted(commands),
        "stanza": stanza,
    }


# -- run stats --------------------------------------------------------------

# The windows the page offers: the CLI's ``--since`` spelling.
STATS_WINDOWS = ("24h", "7d", "30d")
# A finished read answers every request for this long.
STATS_TTL_S = 30.0
# A failed read is remembered this long, so a broken log is not re-read on
# every poll.
STATS_ERROR_TTL_S = 5.0
# How long a request waits for the first read before it answers "loading":
# a small library finishes inside it, a large one is polled for.
FIRST_WAIT_S = 0.25
RECENT_RUNS = 5

_STATS_LOCK = threading.Lock()
# (home, window) -> {"payload", "error", "at", "thread"}: the last good
# payload, the last failure, when the last read ended, and the read in
# flight, if any.
_STATS: dict[tuple[str, str], dict] = {}


def stats_view(home: Path, window: str, force: bool = False) -> dict:
    """``GET ?stats=<window>[&refresh=1]``: the payload, or ``{"state":
    "loading"}`` while the first read runs. ``refreshing`` is true while a
    newer read is running behind an older answer, so a client keeps
    asking until it is false; ``force`` (the Refresh button) starts that
    read even inside the TTL. A request never runs the read itself, and
    at most one runs per (home, window)."""
    if window not in STATS_WINDOWS:
        raise PageError(422, "bad_window",
                        f"window must be one of {', '.join(STATS_WINDOWS)}.")
    key = (str(home), window)
    with _STATS_LOCK:
        entry = _STATS.setdefault(key, {})
        thread = entry.get("thread")
        running = thread is not None and thread.is_alive()
        ttl = STATS_TTL_S if "error" not in entry else STATS_ERROR_TTL_S
        fresh = "at" in entry and time.monotonic() - entry["at"] < ttl
        if (not fresh or force) and not running:
            thread = threading.Thread(
                target=_stats_worker, args=(key, home, window),
                name="hooks-stats", daemon=True)
            entry["thread"] = thread
            thread.start()
            running = True
        has_payload = "payload" in entry
    # Only a read with nothing to show yet is waited for: once there are
    # numbers, a refresh runs behind them and they are served meanwhile.
    if running and not has_payload:
        thread.join(FIRST_WAIT_S)
    with _STATS_LOCK:
        entry = _STATS.get(key, {})
        if "payload" in entry:
            thread = entry.get("thread")
            return {**entry["payload"],
                    "refreshing": thread is not None and thread.is_alive()}
        if "error" in entry:
            raise PageError(503, "stats_unavailable", entry["error"])
    return {"state": "loading", "window": window}


def _stats_worker(key: tuple[str, str], home: Path, window: str) -> None:
    try:
        payload, error = build_stats_payload(home, window), None
    except Exception as exc:  # noqa: BLE001 - kept for the page, never raised
        payload, error = None, str(exc) or type(exc).__name__
    with _STATS_LOCK:
        entry = _STATS.setdefault(key, {})
        if error is None:
            entry["payload"] = payload
            entry.pop("error", None)
        else:
            entry["error"] = error
        entry["at"] = time.monotonic()
        entry.pop("thread", None)


def build_stats_payload(home: Path, window: str, now: float | None = None) -> dict:
    """What ``swe hooks stats --json`` and ``swe hooks log --json`` show
    for ``window``, one row per hook script with the harnesses summed:
    the counts, the top reasons and the newest runs of each."""
    from swe.hooks import events as _events
    from swe.hooks import stats_commands as _stats

    now = time.time() if now is None else now
    since = _events.parse_since(window, now)
    data = _stats.collect(home, since, True, now)
    events = data["events"]
    # Blank the harness so build_stats folds each hook into one row.
    merged = _events.build_stats([{**e, "harness": ""} for e in events])
    harnesses: dict[str, set] = {}
    newest: dict[str, list] = {}
    for event in sorted(events, key=lambda e: e["ts"], reverse=True):
        harnesses.setdefault(event["hook"], set()).add(event["harness"])
        runs = newest.setdefault(event["hook"], [])
        if len(runs) < RECENT_RUNS:
            runs.append(_stats._event_json(event))
    rows = []
    for row in merged["hooks"]:
        row = {k: v for k, v in row.items() if k != "harness"}
        row["harnesses"] = sorted(h for h in harnesses.get(row["hook"], ()) if h)
        row["recent"] = newest.get(row["hook"], [])
        rows.append(row)
    return {
        "state": "ready",
        "window": window,
        "since": since,
        "until": now,
        "sources": data["sources"],
        "totals": merged["totals"],
        "hooks": rows,
    }
