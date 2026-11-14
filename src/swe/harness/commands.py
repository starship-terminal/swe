"""Harness registry commands: list, info, add, remove, use, check, tags, aliases, star."""

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from swe.console import c, cpad, elide, fit_widths, terminal_width, truncate
from swe.flags import expand_value_flags
from swe.harness.columns import (
    COLUMNS,
    DEFAULT_COLUMNS,
    list_section,
    load_columns,
    load_window,
    next_window,
    save_columns,
    save_window,
    window_label,
)
from swe.harness.models import (
    ArchiveReply,
    DoctorReport,
    EditReply,
    HarnessRow,
    StateReply,
    display_name,
    primary_color,
)
from swe.harness.registry import (
    archived_names,
    load_registry,
    load_registry_readonly,
    resolve,
    save_registry,
    state_of,
)
from swe.init.layout import link_states
from swe.harness.stars import (
    is_starred,
    load_stars,
    star as star_name,
    toggle_star,
    unstar as unstar_name,
)
from swe.harness.tools import extract_version_number, is_installed, live_version
from swe.configuration import ConfigurationError
from swe.json_output import emit, error_stream, take_json_flag
from swe.prompt import read_line
from swe.table import Table


# Least to most, so the colour tracks the ordering rather than being
# assigned per word.
_USAGE_COLOR = {
    "unknown": "dim", "none": "dim", "trial": "yellow",
    "used": "cyan", "heavy": "green",
}


# The longest level, so the column is exactly as wide as it needs to be.
_USAGE_WIDTH = max(len(x) for x in _USAGE_COLOR)


def _usage_cell(entry) -> str:
    """How much an archived harness got used. Blank for an active one."""
    if not entry:
        return ""
    level = entry.get("usage") or "unknown"
    return c(_USAGE_COLOR.get(level, "dim"), level)


def _broken_tools():
    """Tools whose session count is unknown because their parser crashed."""
    from swe.sessions.usage import broken_tools

    return broken_tools()


def _path_memo():
    """``is_installed`` remembered for one pass: ``swe list`` asks about
    each command for its row and again for the installed count (#1537).
    Scoped to the pass, never the process, so the long-running hub still
    sees a harness installed after it started."""
    seen: dict[str, bool] = {}

    def on_path(command: str) -> bool:
        if command not in seen:
            seen[command] = is_installed(command)
        return seen[command]

    return on_path


# Version probes run side by side: each is up to three subprocesses with
# a timeout, and `swe check` waited on them one harness at a time.
_VERSION_PROBE_WORKERS = 8


def _live_versions(commands, on_path) -> dict[str, str | None]:
    """``live_version`` for every installed command in ``commands``,
    probed on a small thread pool (#1537)."""
    from concurrent.futures import ThreadPoolExecutor

    installed = sorted(cmd for cmd in commands if on_path(cmd))
    if not installed:
        return {}
    with ThreadPoolExecutor(max_workers=min(_VERSION_PROBE_WORKERS,
                                            len(installed))) as pool:
        return dict(zip(installed, pool.map(live_version, installed)))


_CONFIGURED = object()


def _session_counts(days=_CONFIGURED):
    """Session counts over ``days`` (``None`` is every session ever), or
    over the configured window when ``days`` is left out."""
    from swe.sessions.usage import session_counts

    return session_counts(load_window() if days is _CONFIGURED else days)


def _sort_tools(tools: dict, counts: dict[str, int], stars: list[str]):
    """Starred first, then unstarred. Each group by 100d usage, then name.

    Starred rows used to hold their pin order, which meant a favourite you had
    not touched in months sat above one you use daily. Both groups now answer
    the same question, "what am I actually using", with the star deciding only
    which block you are in.
    """
    starred = set(stars)

    def key(item):
        name = item[0]
        return (0 if name in starred else 1, -counts.get(name, 0), name)

    return sorted(tools.items(), key=key)


# Which instruction filename each harness insists on, so the column shows
# what is actually linked rather than a bare tick.
_AGENTS_FILENAMES = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "cursor": "AGENTS.md",
    "gemini": "GEMINI.md",
    "qwen-code": "QWEN.md",
    "crush": "CRUSH.md",
    "opencode": "AGENTS.md",
    "droid": "AGENTS.md",
}


# Link-state glyphs for `swe list --links`. "linked" is the goal state;
# "conflict" means a real file is sitting where the symlink should be;
# "skipped" means the harness is not installed. A tool with no known
# instruction-file convention gets None and renders as a dim dot.
_LINK_GLYPH = {
    "linked": ("green", "\u2713"),
    "create": ("yellow", "\u25cb"),
    "relink": ("yellow", "\u21bb"),
    "conflict": ("red", "\u2717"),
    "skipped": ("dim", "\u00b7"),
    # Holds skills that exist nowhere else, so swe deliberately leaves it
    # alone. Not an error, and not a tick either.
    "keep": ("yellow", "\u25cb"),
    # A real directory whose contents are all duplicates or empty, so init can
    # replace it with the link without losing anything.
    "absorb": ("yellow", "\u25cb"),
    # Listed in ~/.swe/.linkignore: the user asked init to leave it alone.
    "ignored": ("dim", "\u2013"),
}


def _link_cell(state, label):
    """Render one link column: glyph plus what it links."""
    if state is None:
        return c("dim", "\u00b7")
    colour, glyph = _LINK_GLYPH.get(state, ("dim", "?"))
    return c(colour, glyph) + " " + c("dim" if state != "linked" else "green", label)



def cmd_list_edit(args=None) -> int:
    """Pick which columns `swe list` shows."""
    from swe.multiselect import Choice, multiselect

    args = list(args or [])
    if args and args[0] in ("-h", "--help", "help"):
        print(f"\n  {c('bold', 'swe list edit')} — choose the columns swe list shows\n")
        print(f"  {c('dim', 'space toggles · ← → cycles a value (e.g. the session window) · a selects all')}")
        print(f"  {c('dim', 'n clears · enter saves · q cancels')}")
        print(f"  {c('dim', 'NAME and the favourite marker are always shown.')}\n")
        return 0
    if args and args[0] == "--reset":
        if len(args) > 1:
            print(c("red", f"  Unexpected args: {' '.join(args[1:])}"))
            return 1
        save_columns(DEFAULT_COLUMNS)
        save_window(100)
        print(f"  {c('green', 'reset')} {c('dim', 'to ' + ', '.join(DEFAULT_COLUMNS))}")
        return 0
    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1

    current = load_columns()
    window = load_window()
    choices = []
    for col in COLUMNS:
        if col.key == "sess":
            choices.append(Choice(
                col.key, col.label, col.about, col.locked,
                value=window, cycle=next_window, render_value=window_label))
        else:
            choices.append(Choice(col.key, col.label, col.about, col.locked))
    picked = multiselect(choices, current, title="  Columns for swe list")
    if picked is None:
        print(f"  {c('dim', 'cancelled, nothing changed')}")
        return 0

    chosen_window = next(
        (ch.value for ch in choices if ch.key == "sess"), window)
    if chosen_window != window:
        save_window(chosen_window)
        print(f"  {c('green', 'window')} {c('dim', window_label(window) + ' -> ' + window_label(chosen_window))}")
    saved = save_columns(picked)
    added = [k for k in saved if k not in current]
    removed = [k for k in current if k not in saved]
    print(f"  {c('green', 'saved')}  {c('dim', ', '.join(saved))}")
    if added:
        print(f"  {c('dim', '+ ' + ', '.join(added))}")
    if removed:
        print(f"  {c('dim', '- ' + ', '.join(removed))}")
    if "rate" in saved:
        note = ("QUOTA fetches over the network, so swe list will be "
                "slower on a cold cache")
        print(f"  {c('yellow', 'note')} {c('dim', note)}")
    return 0


def cmd_list_legend(args=None) -> int:
    """Explain the glyphs used by the swe list columns.

    The columns render as single characters with no key anywhere, so a
    reader who forgot what a yellow circle meant had nowhere to look.
    """
    from swe.harness.columns import load_window, window_label

    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1

    label = window_label(load_window())
    print(f"\n  {c('bold', f'{label} column')}\n")
    print(f"  {c('green', '12'.rjust(4))}  {c('dim', f'sessions in the last {label}')}")
    print(f"  {c('dim', '0'.rjust(4))}  {c('dim', 'the parser ran and found none')}")
    print(f"  {c('red', '!'.rjust(4))}  {c('dim', 'the parser failed, so the count is unknown')}")
    print(f"  {c('dim', '—'.rjust(4))}  {c('dim', 'no session parser exists for this harness yet')}")
    print(f"\n  {c('dim', 'swe session shows what a ! actually raised.')}")

    print(f"\n  {c('bold', 'Marker column')}\n")
    print(f"  {c('neon_pink', '★'.rjust(4))}  {c('dim', 'favourited: pinned to the top, neon border, and the only tools --usage polls')}")
    print(f"  {c('yellow', '▪'.rjust(4))}  {c('dim', 'archived: hidden from swe list, nothing uninstalled, reason recorded')}")
    print(f"        {c('dim', '--scope=all (or =archived) shows it again')}")

    from swe.harness.archive import USAGE_ABOUT, USAGE_LEVELS

    print(f"\n  {c('bold', 'USAGE column')}  {c('dim', '(archived harnesses only)')}\n")
    for level in reversed(USAGE_LEVELS):
        print(f"  {c(_USAGE_COLOR.get(level, 'dim'), level.rjust(8))}  "
              f"{c('dim', USAGE_ABOUT[level])}")
    print(f"\n  {c('dim', 'Derived from lifetime sessions; override with')} "
          f"{c('cyan', 'swe hs archive <name> --usage=<level>')}")
    print(f"  {c('dim', 'USAGE, ARCHIVED and REASON appear only when a row is archived.')}")
    print(f"  {c('dim', 'Turn them on with')} {c('cyan', 'swe list edit')}"
          f"{c('dim', ', or read them with')} {c('cyan', 'swe hs archive')}{c('dim', '.')}")

    print(f"\n  {c('bold', 'QUOTA column')}  {c('dim', '(starred harnesses only)')}\n")
    print(f"  {'●◕◑◔○'}   {c('dim', 'share of the tightest quota window left, green when full to red when empty')}")
    print(f"          {c('dim', 'the dim tail is the window and its reset countdown (5h / 7d / 7ds for claude)')}")
    print(f"  {c('dim', 'no-sub'.ljust(8))} {c('dim', 'the endpoint is not polled for this tool (e.g. no subscription)')}")
    print(f"  {c('red', 're-login'.ljust(8))} {c('dim', 'token expired; sign in to the harness again')}")
    print(f"  {c('yellow', '…'.ljust(8))} {c('dim', 'the fetch did not finish inside the 2s deadline; swe list -n retries it')}")
    print(f"  {c('dim', '—'.ljust(8))} {c('dim', 'no usage endpoint, or it returned nothing')}")

    print(f"\n  {c('bold', 'AGENTS.MD and SKILLS glyphs')}\n")
    rows = [
        ("linked", "points at the shared copy in ~/.swe"),
        ("relink", "is a symlink, but aimed somewhere other than ~/.swe"),
        ("create", "nothing there yet; swe init would create the link"),
        ("absorb", "a real directory, but its contents are all duplicates or empty"),
        ("keep", "a real directory holding files that exist nowhere else, left alone"),
        ("conflict", "a real file sits where the link should go; needs --force"),
        ("skipped", "harness is not installed"),
        ("ignored", "listed in ~/.swe/.linkignore, left alone on purpose"),
    ]
    for state, meaning in rows:
        colour, glyph = _LINK_GLYPH.get(state, ("dim", "?"))
        print(f"  {c(colour, glyph)}  {c('bold', state.ljust(9))} {c('dim', meaning)}")
    print(f"  {c('dim', '·')}  {c('bold', 'n/a'.ljust(9))} "
          f"{c('dim', 'swe knows no instruction filename for this harness')}")
    print(f"\n  {c('dim', 'swe find skills -r shows the same states as words, per path.')}\n")
    return 0


LIST_SCOPES = ("active", "archived", "all")


def _fmt_when(stamp: str) -> str:
    """A stored timestamp as a plain date, or the raw value if unparseable."""
    if not stamp:
        return "—"
    try:
        from datetime import datetime

        return datetime.fromisoformat(stamp).strftime("%Y-%m-%d")
    except ValueError:
        return stamp[:10]


def cmd_harness_edit(args=None):
    """Review every harness at once: active, starred, or archived.

    The one-at-a-time commands are fine for a single decision. Going
    through the whole registry with them means remembering which you have
    already judged, so this puts them all on one screen.

    Archiving here asks for a reason afterwards, one harness at a time. A
    blank answer cancels that archive rather than recording a bare date:
    an archive with no reason is the thing you cannot act on later, which
    is the whole point of keeping the record.
    """
    from swe.harness.archive import archive, load_archive, unarchive
    from swe.multiselect import StateChoice, statepicker

    args = list(args or [])
    if args and args[0] in ("-h", "--help", "help"):
        print(f"\n  {c('bold', 'swe harness edit')} — review every harness at once\n")
        print(f"  {c('dim', 'space cycles active → starred → archived · s / x / c jump straight to one')}")
        print(f"  {c('dim', 'enter saves · q cancels · archiving then asks why, one at a time')}")
        print(f"  {c('dim', 'A blank reason cancels that archive; the record is the point.')}\n")
        return 0
    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1

    tools = load_registry()
    if not tools:
        print(c("dim", "  no harnesses registered yet — try swe harness discover"))
        return 0

    starred = set(load_stars())
    archived = load_archive()

    def state_of(name):
        if name in archived:
            return "archived"
        return "starred" if name in starred else "active"

    counts = _session_counts()
    choices = [
        StateChoice(
            key=name,
            label=name,
            state=state_of(name),
            about=(archived[name]["reason"] if name in archived
                   else f"{counts.get(name, 0)} sessions"),
        )
        for name in sorted(tools, key=lambda n: (-counts.get(n, 0), n))
    ]
    before = {ch.key: ch.state for ch in choices}

    picked = statepicker(choices, title="  Harnesses")
    if picked is None:
        print(f"  {c('dim', 'cancelled, nothing changed')}")
        return 0

    changed = [ch for ch in picked if ch.state != before[ch.key]]
    if not changed:
        print(f"  {c('dim', 'no changes')}")
        return 0

    # Stars and restores apply straight away. Archives wait for a reason.
    to_archive = []
    for ch in changed:
        was, now = before[ch.key], ch.state
        if was == "archived" and now != "archived":
            unarchive(ch.key)
        if was == "starred" and now != "starred":
            unstar_name(ch.key)
        if now == "starred" and was != "starred":
            star_name(ch.key)
        if now == "archived":
            to_archive.append(ch.key)

    applied = [ch for ch in changed if ch.state != "archived"]
    for ch in applied:
        colour, glyph = ("neon_pink", "\u2605") if ch.state == "starred" else ("dim", "\u00b7")
        print(f"  {c(colour, glyph)} {ch.key} {c('dim', '→ ' + ch.state)}")

    if not to_archive:
        return 0

    print(f"\n  {c('bold', f'Why are you archiving these? ({len(to_archive)})')}")
    print(f"  {c('dim', 'blank cancels that one — an archive with no reason is not worth keeping')}\n")

    kept, skipped = [], []
    for name in to_archive:
        try:
            reason = read_line(f"  {c('yellow', '▪')} {c('bold', name)}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            skipped.extend(to_archive[len(kept) + len(skipped):])
            break
        if not reason:
            skipped.append(name)
            continue
        archive(name, reason)
        kept.append(name)

    print()
    for name in kept:
        print(f"  {c('yellow', '▪')} Archived {name}")
    for name in skipped:
        print(f"  {c('dim', '·')} {c('dim', f'{name} left active — no reason given')}")
    print()
    return 0


def cmd_archive(args):
    """Shelve a harness you have evaluated and rejected, with the reason why.

    Not a delete. `swe remove` forgets the harness, which loses the fact
    that you tried it, so it reads as untested later and gets reinstalled.
    """
    from swe.harness.archive import (
        USAGE_LEVELS,
        archive,
        load_archive,
        normalise_usage,
        unarchive,
    )

    args, json_view = take_json_flag(list(args or []))
    # Under --json a refusal must leave stdout empty: the app decodes it whole.
    err = error_stream(json_view)
    entries = load_archive()

    if json_view and (not args or args[0] in ("list", "ls")):
        return emit({"archived": entries})
    if not args or args[0] in ("list", "ls"):
        if not entries:
            print(f"\n  {c('dim', 'nothing archived')}")
            print(f"  {c('dim', 'swe hs archive <name> [why]   shelve a harness you have ruled out')}\n")
            return 0
        print(f"\n  {c('bold', f'Archived ({len(entries)})')}\n")
        width = max(len(n) for n in entries) + 2
        for name, e in sorted(entries.items(),
                              key=lambda kv: kv[1].get("archived_at", ""),
                              reverse=True):
            reason_w = max(16, terminal_width() - width - 12 - 9 - 6)
            reason = elide(e["reason"] or "no reason given", reason_w)
            print(f"  {c('dim', '▪')} {c('bold', name.ljust(width))}"
                  f"{c('dim', _fmt_when(e['archived_at']).ljust(12))}"
                  f"{c(_USAGE_COLOR.get(e['usage'], 'dim'), e['usage'].ljust(9))}"
                  f"{c('dim', reason)}")
        print(f"\n  {c('dim', 'swe hs archive <name>   restore  ·  swe list --scope=all   show them inline')}\n")
        return 0

    if args[0] in ("-h", "--help", "help"):
        print(f"\n  {c('bold', 'swe harness archive')} — shelve a harness you have ruled out\n")
        print(f"  {c('cyan', 'swe hs archive')}                  List archived harnesses")
        print(f"  {c('cyan', 'swe hs archive <name> [why]')}     Archive, with an optional reason")
        print(f"  {c('cyan', 'swe hs archive <name>')}           Restore one that is archived")
        print(f"  {c('cyan', 'swe hs archive <name> --usage=<level>')}  Override the guessed usage level")
        print(f"\n  {c('dim', 'Archived harnesses drop out of swe list. --scope=all brings them back,')}")
        print(f"  {c('dim', 'and --scope=archived shows only those, with the date and reason.')}")
        print(f"  {c('dim', 'Nothing is uninstalled — archiving only hides the row and records why.')}")
        print(f"\n  {c('dim', '--usage levels: ' + ', '.join(USAGE_LEVELS))}\n")
        return 0

    # --usage=<level> overrides what the session count implies. The count is
    # wrong in both directions: a harness with no parser reads as unknown
    # however much you used it, and a high count can come from one long
    # evaluation rather than real adoption.
    try:
        args = expand_value_flags(args, {"--usage"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1
    usage = None
    i = 0
    while i < len(args):
        if args[i] == "--usage":
            usage = args[i + 1]
            del args[i : i + 2]
        else:
            i += 1
    if usage is not None and normalise_usage(usage, "") == "":
        print(c("red", f"  Unknown usage level: {usage or '(empty)'}. "
                       f"Use {', '.join(USAGE_LEVELS)}."), file=err)
        return 1
    if not args:
        print(c("red", "  Usage: swe hs archive <name> [why] [--usage=<level>]"), file=err)
        return 1

    tools = load_registry()
    key = args[0]
    reason = " ".join(args[1:]).strip()

    name = resolve(tools, key)
    if not name:
        # An archived harness may since have left the registry; it must
        # still be restorable by the name it was filed under.
        if key in entries:
            name = key
        else:
            print(c("red", f"  Tool '{key}' not found. Try 'swe list'."), file=err)
            return 1

    if name in entries and not reason and usage is None:
        old = unarchive(name)
        if json_view:
            return emit(ArchiveReply(name, _registry_state(name), None).to_json())
        print(f"  {c('green', '✓')} Restored {c('bold', name)}")
        print(f"    {c('dim', 'was archived ' + _fmt_when(old['archived_at']))}"
              f"{c('dim', ': ' + old['reason'] if old['reason'] else '')}")
        return 0

    entry = archive(name, reason, usage=usage)
    if json_view:
        return emit(ArchiveReply(name, "archived", entry).to_json())
    verb = "Re-archived" if name in entries else "Archived"
    print(f"  {c('dim', '▪')} {verb} {c('bold', name)} "
          f"{c('dim', _fmt_when(entry['archived_at']))} "
          f"{c(_USAGE_COLOR.get(entry['usage'], 'dim'), entry['usage'])}")
    if entry["reason"]:
        print(f"    {c('dim', entry['reason'])}")
    else:
        print(f"    {c('dim', 'no reason given — swe hs archive ' + name + ' <why> to add one')}")
    return 0


# Braille spinner for the rate-limit fetch wait. The lead-in delay keeps a
# cache hit (or a fast fetch) from flashing a frame nobody can read.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_DELAY = 0.2
_SPINNER_FRAME_SECONDS = 0.08


def _fetch_rate_limits(refresh: bool, tool_names: set[str], wait: bool = False,
                       refresh_failed: bool = False,
                       cache_ttl: float | None = None,
                       no_reading: dict | None = None,
                       issues: dict | None = None) -> dict:
    """Rate limits for the REMAINING column.

    On a terminal the fetch runs behind a spinner and workers get a longer
    deadline than the pipeline budget, so a slow provider resolves to a
    figure instead of the ``…`` timeout marker. Piped output keeps the
    short cap and prints once, static.

    ``wait`` gives that longer deadline without a spinner. ``--json`` asks
    for it: the macOS app reads limits that way from a fresh process each
    time, where a provider's first fetch often takes longer than the short
    cap, so it would only ever see the last cached reading, hours old.

    ``no_reading`` and ``issues`` are passed through to
    ``get_all_rate_limits``.
    """
    extra = {}
    if no_reading is not None:
        extra["no_reading"] = no_reading
    if issues is not None:
        extra["issues"] = issues
    from swe.harness.rate_limits import (
        _RATE_LIMIT_FETCH_DEADLINE_TTY,
        get_all_rate_limits,
    )

    if wait:
        return get_all_rate_limits(
            use_cache=not refresh, tool_names=tool_names,
            deadline=_RATE_LIMIT_FETCH_DEADLINE_TTY,
            refresh_failed=refresh_failed, cache_ttl=cache_ttl, **extra,
        )
    if not sys.stdout.isatty():
        return get_all_rate_limits(
            use_cache=not refresh, tool_names=tool_names,
            refresh_failed=refresh_failed, cache_ttl=cache_ttl, **extra,
        )

    holder: dict = {}

    def work() -> None:
        try:
            holder["result"] = get_all_rate_limits(
                use_cache=not refresh,
                tool_names=tool_names,
                deadline=_RATE_LIMIT_FETCH_DEADLINE_TTY,
                refresh_failed=refresh_failed, cache_ttl=cache_ttl, **extra,
            )
        except Exception as exc:
            # Surface the failure exactly as the non-TTY path would instead
            # of silently rendering a blank column.
            holder["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(_SPINNER_DELAY)
    spin = sys.stderr.isatty()
    frames = 0
    while worker.is_alive():
        if spin:
            sys.stderr.write(
                "\r" + c("dim", f"{_SPINNER[frames % len(_SPINNER)]} "
                                 "fetching usage…"))
            sys.stderr.flush()
        frames += 1
        worker.join(_SPINNER_FRAME_SECONDS)
    if frames and spin:
        sys.stderr.write("\r" + " " * 20 + "\r")
        sys.stderr.flush()
    if "error" in holder:
        raise holder["error"]
    return holder["result"]


def _kept_fields(previous: dict | None) -> dict:
    """Fields a re-add carries over from the entry it replaces.

    ``swe add`` rewrites a whole entry; state, pins, capabilities and
    any other field the command does not own are user decisions, so a
    re-add must not erase them.
    """
    previous = previous or {}
    owned = {"command", "description", "version", "tags", "aliases", "added"}
    return {k: v for k, v in previous.items() if k not in owned}


def cmd_list(args):
    args, json_view = take_json_flag(list(args or []))
    # Under --json a refusal must leave stdout empty: the app decodes it whole.
    err = error_stream(json_view)
    if json_view and args and args[0] in ("edit", "legend", "--legend", "key"):
        # Refused before dispatch: `edit --reset` writes config, and neither
        # subcommand has a JSON form.
        print(c("red", f"swe list {args[0]} has no --json output"), file=err)
        return 1
    # Subcommand dispatch stays on the untouched argv. The subcommands do
    # their own arg checking, and keeping them first preserves `swe list
    # -u edit` treating "edit" as a tag filter rather than opening the
    # column editor once -u is consumed.
    if args and args[0] == "edit":
        return cmd_list_edit(args[1:])
    if args and args[0] in ("legend", "--legend", "key"):
        return cmd_list_legend(args[1:])

    # Boolean flags are matched on the raw argv, before expand_value_flags:
    # an expanded --scope=-u pair would smuggle -u in as a real flag, and a
    # boolean sitting where a value belongs would be eaten instead.
    refresh_flags = {"--refresh", "-r", "-n"}
    refresh = any(arg in refresh_flags for arg in args)
    args = [arg for arg in args if arg not in refresh_flags]

    # --refresh-failed refetches only the providers whose cached reading
    # failed (timeout, auth-required, a window left unread); healthy ones
    # are still served from cache. --refresh overrides it.
    refresh_failed = "--refresh-failed" in args
    args = [arg for arg in args if arg != "--refresh-failed"]

    # Usage is opt-in. Rate limits are the only part of `swe list` that
    # touches the network, and on a cold cache they cost ~850ms against
    # ~15ms for everything else combined. Asking for them explicitly keeps
    # the default listing instant. Refreshing implies wanting to see them.
    usage_flags = {"--usage", "-u"}
    usage_view = refresh or refresh_failed or any(arg in usage_flags for arg in args)
    args = [arg for arg in args if arg not in usage_flags]

    # --links swaps in swe link status. It is a different question about
    # the same rows, so it replaces columns rather than widening an already
    # wide table.
    link_flags = {"--links", "-L"}
    links_view = any(arg in link_flags for arg in args)
    args = [arg for arg in args if arg not in link_flags]
    if links_view:
        usage_view = False

    try:
        args = expand_value_flags(args, {"--scope", "--exclude", "--cache-ttl"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1

    if refresh:
        from swe.sessions.aggregator import invalidate_cache as _inv_sessions
        from swe.sessions.usage import invalidate_counts_cache

        _inv_sessions()
        # The 100d counts cache holds for a day, so --refresh has to clear it
        # too or the flag would silently do nothing for that column.
        invalidate_counts_cache()

    cache_ttl: float | None = None
    # --scope mirrors swe find: active (default) hides what you shelved,
    # archived shows only that, all shows everything with a marker.
    scope = "active"
    # --exclude=a,b keeps those rows but never polls their usage. The macOS
    # app fetches Claude and Codex per account itself, so asking swe for them
    # too would double the load on endpoints that rate-limit.
    excluded: set[str] = set()
    i = 0
    while i < len(args):
        if args[i] == "--scope":
            scope = args[i + 1]
            del args[i : i + 2]
        elif args[i] == "--cache-ttl":
            try:
                cache_ttl = float(args[i + 1])
            except ValueError:
                cache_ttl = 0.0
            if not cache_ttl > 0:
                print(c("red", f"  Bad --cache-ttl: {args[i + 1] or '(empty)'}. "
                               "Give a positive number of seconds."), file=err)
                return 1
            del args[i : i + 2]
        elif args[i] == "--exclude":
            excluded |= {name.strip() for name in args[i + 1].split(",") if name.strip()}
            del args[i : i + 2]
        else:
            i += 1
    if scope not in LIST_SCOPES:
        print(f"Unknown scope: {scope or '(empty)'}. Use {', '.join(LIST_SCOPES)}.",
              file=err)
        return 1

    bad = next((a for a in args if a.startswith("-")), None)
    if bad:
        print(c("red", f"Unknown flag: {bad}"), file=err)
        return 1
    if len(args) > 1:
        print(c("red", f"Unexpected args: {' '.join(args[1:])}"), file=err)
        return 1

    tools = load_registry()

    from swe.harness.archive import load_archive

    archived = load_archive()
    # A registry starts empty and fills up from what discovery finds, so a
    # fresh install lands here with nothing to render. Say what to run
    # instead of printing a header over an empty table.
    if not tools and not archived:
        if json_view:
            return emit({"harnesses": []})
        print(f"\n  {c('dim', 'No harnesses registered yet.')}")
        print(f"  {c('dim', 'Run')} {c('cyan', 'swe discover --apply')} "
              f"{c('dim', 'to register what is installed here.')}\n")
        return 0
    if scope == "active":
        tools = {n: t for n, t in tools.items() if n not in archived}
    elif scope == "archived":
        tools = {n: t for n, t in tools.items() if n in archived}
        # An archived harness can outlive its registry entry; it should
        # still be listed under --scope=archived rather than vanishing.
        for name in archived:
            # No registry entry left, so there is no command to probe; the
            # row still has to render, showing what was archived and when.
            tools.setdefault(name, {"command": name, "description": "",
                                    "aliases": [], "tags": []})

    tag_filter = args[0] if args else None
    settings = list_section()
    columns, window = load_columns(settings), load_window(settings)
    counts = _session_counts(window)
    broken = _broken_tools() if "sess" in set(columns) else set()
    _sess_label = window_label(window)
    # USAGE is only ever filled in for an archived harness, so in the
    # default active scope it rendered as a wholly blank column.
    shows_usage = any(n in archived for n in tools)
    stars = load_stars()
    starred_set = set(stars)

    if json_view:
        no_reading: dict[str, str] = {}
        issues: dict[str, str] = {}
        rate_limits = (
            _fetch_rate_limits(
                refresh=refresh,
                tool_names={name for name in starred_set
                            if name in tools and name not in excluded},
                wait=True,
                refresh_failed=refresh_failed, cache_ttl=cache_ttl,
                no_reading=no_reading,
                issues=issues,
            )
            if usage_view
            else {}
        )
        # The field is named for a fixed window, whatever window the table
        # is set to show.
        counts_100d = _session_counts(100)
        return emit({"harnesses": [
            HarnessRow.of(name, info, starred_set, archived, counts_100d,
                          rate_limits.get(name), no_reading.get(name),
                          issue_status=issues.get(name)).to_json()
            for name, info in tools.items()
            if not tag_filter or tag_filter in info.get("tags", [])
        ]})

    # Fetch rate limits (cached 5min by default, refresh flags bypass;
    # override TTL with SWE_RATE_LIMITS_TTL=<seconds>)

    # Which columns will actually render, so the data behind them is fetched
    # exactly when it is needed. Gating on the flags alone was wrong once the
    # set became configurable: a saved AGENTS.MD column rendered every row as
    # "no known convention" because nothing had populated the link state.
    wanted = set(columns)
    if links_view:
        wanted |= {"agents", "skills"}
    if usage_view:
        wanted |= {"sess", "rate"}

    rate_limits = (
        _fetch_rate_limits(
            refresh=refresh,
            tool_names={name for name in starred_set
                            if name in tools and name not in excluded},
            refresh_failed=refresh_failed, cache_ttl=cache_ttl,
        )
        if "rate" in wanted
        else {}
    )
    link_status = link_states() if wanted & {"agents", "skills"} else {}

    print(f"\n{c('bold', 'AI Coding Tools')}\n")

    # Widths the shrinking columns will actually settle on, so the
    # description can be given the remainder rather than guessing.
    names = list(tools)
    name_w = max([len("NAME")] + [len(n) for n in names]) if names else 16
    command_w = max([len("COMMAND")] +
                    [len(str(i.get("command") or "")) for i in tools.values()]) if names else 18
    version_w = min(12, max([len("VERSION")] +
                    [len(str(i.get("version") or "")) for i in tools.values()])) if names else 12
    aliases_w = min(12, max([len("ALIASES")] + [
        len(", ".join(a for a in i.get("aliases", []) if a != n))
        for n, i in tools.items()])) if names else 12
    rendered = set()

    table = Table(column_gap=" │ ")
    _head = wanted
    rendered.add("mark")
    table.add_column("mark", "", width=2, kind="preformatted")
    rendered.add("name")
    table.add_column("name", "NAME", width=16, kind="text", fit="shrink")
    if "command" in _head:
        rendered.add("command")
        table.add_column("command", "COMMAND", width=18, kind="text",
                         fit="shrink")
    if "version" in _head:
        rendered.add("version")
        table.add_column("version", "VERSION", width=12, kind="text",
                         fit="shrink")
    if "aliases" in _head:
        rendered.add("aliases")
        table.add_column("aliases", "ALIASES", width=12, kind="list",
                         color="cyan", empty="—", fit="shrink")
    if "sess" in wanted:
        rendered.add("sess")
        table.add_column("sess", _sess_label, width=len(_sess_label),
                         kind="preformatted", right=True, empty="—")
    if "usage" in wanted and shows_usage:
        rendered.add("usage")
        table.add_column("usage", "USAGE", width=_USAGE_WIDTH,
                         kind="preformatted")
    if "archived" in wanted and shows_usage:
        rendered.add("archived")
        table.add_column("archived", "ARCHIVED", width=10, kind="text")

    if "rate" in wanted:
        table.add_column("rate", "QUOTA", width=5, kind="preformatted")
    if "agents" in wanted:
        rendered.add("agents")
        table.add_column("agents", "AGENTS.MD", width=22, kind="preformatted")
    if "skills" in wanted:
        rendered.add("skills")
        table.add_column("skills", "SKILLS", width=12, kind="preformatted")
    if "inst" in wanted:
        rendered.add("inst")
        table.add_column("inst", "INST", width=4, kind="preformatted")
    # Whichever of DESCRIPTION and REASON comes last absorbs the slack.
    # REASON wins when both are on: in an archived view, why you shelved a
    # harness matters more than what its vendor calls it.
    show_reason = "reason" in wanted and shows_usage
    used = sum(w for key, w in (
        ("mark", 2), ("name", name_w), ("command", command_w),
        ("version", version_w), ("aliases", aliases_w),
        ("inst", 4), ("sess", len(_sess_label)), ("usage", _USAGE_WIDTH),
        ("archived", 10), ("rate", 5), ("agents", 22), ("skills", 12),
    ) if key in rendered)
    gaps = 3 * (len(rendered) + (1 if "desc" in wanted else 0) + (1 if show_reason else 0))
    slack = terminal_width() - used - gaps - 2

    if "desc" in wanted:
        rendered.add("desc")
        desc_w = min(46, max(20, slack // 2)) if show_reason else max(24, slack)
        table.add_column("desc", "DESCRIPTION", width=desc_w, kind="text",
                         fit="shrink")
        slack -= desc_w
    if show_reason:
        rendered.add("reason")
        table.add_column("reason", "REASON", width=max(24, slack), kind="text",
                         fit="shrink")

    shown_starred = False
    on_path = _path_memo()
    for name, info in _sort_tools(tools, counts, stars):
        if tag_filter and tag_filter not in info.get("tags", []):
            continue

        installed = on_path(info.get("command") or name)
        status = c("green", "✓") if installed else c("red", "✗")
        ver = truncate(info.get("version") or "—", 12)
        aliases = [a for a in info.get("aliases", []) if a != name]
        desc_text = info.get("description", "")

        # desc cell now sits behind the explicit " | " separator, so
        # no extra prepend is needed - Table renders `cell | cell`
        # directly with the gap string itself providing the visual gap.

        # Session column, four states. A crashed parser yields zero
        # sessions, which used to render exactly like a harness you have
        # genuinely never used, so a broken parser could sit unnoticed for
        # months; that is how Cursor showed 0 while holding 84 sessions.
        #   n   green   sessions in the window
        #   0   dim     the parser ran and found none
        #   !   red     the parser failed, so the count is unknown
        #   —   dim     no session parser exists for this harness yet
        if name in broken:
            sess_cell = c("red", "!")
        elif name in counts:
            sess_n = counts.get(name, 0)
            sess_cell = c("green", str(sess_n)) if sess_n > 0 else c("dim", "0")
        else:
            sess_cell = c("dim", "—")

        favourited = name in starred_set
        accent = None
        if favourited:
            shown_starred = True
            mark_cell = c("neon_pink", " \u2605")  # 1 space + ★ = 2 visible (matches column width)
            accent = "neon"
        elif name in archived:
            # Only reachable under --scope=all or =archived; without a marker
            # a shelved harness reads exactly like one still in play.
            mark_cell = c("dim", " \u25aa")        # 1 space + ▪ = 2 visible
        else:
            mark_cell = "  "  # 2 spaces of plain indent

        # Quota cell: format_column returns a one-glyph pie on the
        # red→green ramp plus a dim tail, of variable visible width
        # ("◕ —" = 3 chars, "◔ 7ds:2d4h" = 9). The preformatted kind
        # pads each cell to the settled column width, so a longer
        # payload widens the column once instead of pushing
        # INST/DESCRIPTION right on that row.
        rl = rate_limits.get(name)
        rate_cell = rl.format_column() if rl else c("dim", "—")

        row = {"mark": mark_cell, "name": name}
        if "command" in wanted:
            row["command"] = info.get("command") or "—"
        if "version" in wanted:
            row["version"] = ver
        if "aliases" in wanted:
            row["aliases"] = aliases
        if "inst" in wanted:
            row["inst"] = status
        if "agents" in wanted or "skills" in wanted:
            states = link_status.get(name, {})
            if "agents" in wanted:
                row["agents"] = _link_cell(
                    states.get("agents"), _AGENTS_FILENAMES.get(name, ""))
            if "skills" in wanted:
                row["skills"] = _link_cell(states.get("skills"), "skills/")
        if "sess" in wanted:
            row["sess"] = sess_cell
        if "usage" in wanted and shows_usage:
            row["usage"] = _usage_cell(archived.get(name))
        if "archived" in wanted and shows_usage:
            row["archived"] = _fmt_when((archived.get(name) or {}).get("archived_at", ""))
        if "reason" in wanted and shows_usage:
            row["reason"] = (archived.get(name) or {}).get("reason", "")
        if "rate" in wanted:
            row["rate"] = rate_cell
        if "desc" in wanted:
            # The column now sizes itself, so the cell must not pre-truncate
            # to a narrower fixed number than the column will allow.
            row["desc"] = desc_text

        table.add_row(row, accent=accent)

    for line in table.render():
        print(line)

    print()
    n_inst = sum(1 for i in tools.values()
                 if i.get("command") and on_path(i["command"]))
    n_star = sum(1 for n in tools if n in starred_set)
    # How many rows --scope left out has nothing to do with whether anything
    # is starred. Nesting this under the star legend meant a registry with no
    # favourites silently hid archived harnesses and never said so. Count what
    # the scope filter actually removed, so --scope=archived and --scope=all
    # (which hide nothing) stay quiet.
    hidden = [n for n in archived if n not in tools]
    # The count belongs in the summary itself: "3/3 installed" claiming the
    # registry is fully installed while a shelved fourth row exists is what
    # made `swe list` and `swe check` disagree.
    archived_part = f"  ·  {len(hidden)} archived" if hidden else ""
    hints = "swe use <name>  │  swe hs star <name>  │  swe hs archive <name>  │  swe info <name>"
    # Elide the finished line, not the hints alone: the counts in front of
    # them are part of what has to fit.
    summary = f"  {n_inst}/{len(tools)} installed  ·  {n_star} starred{archived_part}  ·  {hints}"
    print(c("dim", elide(summary, terminal_width())))
    if shown_starred:
        print(f"  {c('neon_pink', '★')} {c('dim', '= favourited (pinned top, neon border)')}")
    if hidden:
        print(f"  {c('dim', '▪')} {c('dim', f'= archived ({len(hidden)} hidden; --scope=all to show)')}")

    all_tags = sorted({t for i in tools.values() for t in i.get("tags", [])})
    # Elide before colouring. elide counts characters, and an ANSI escape
    # is several of them, so eliding a coloured string cuts in the wrong
    # place and can leave a dangling escape.
    tag_str = elide("  ".join(all_tags), max(20, terminal_width() - 10))
    print(f"  {c('dim', 'tags:')}  {c('cyan', tag_str)}\n")
    return 0


def _registry_state(name: str) -> str:
    """A harness's standing after a star toggle or a restore.

    Neither leaves it archived: starring clears an archive record, and a
    restore is the removal of one.
    """
    return "starred" if is_starred(name) else "active"


def cmd_star(args):
    """Favourite / pin harnesses to the top of `swe list`."""
    args, json_view = take_json_flag(list(args or []))
    if json_view and (len(args) != 1 or args[0] in ("clear", "--clear", "list", "ls")):
        # Only the toggle has a JSON form; `swe list --json` already says
        # which harnesses are starred.
        print("swe hs star --json takes exactly one harness name", file=sys.stderr)
        return 1
    err = error_stream(json_view)
    tools = load_registry()

    if not args:
        stars = [s for s in load_stars() if s in tools]
        orphan = [s for s in load_stars() if s not in tools]
        print(f"\n{c('bold', 'Starred harnesses')}\n")
        if not stars and not orphan:
            print(c("dim", "  None yet. Try: swe hs star droid\n"))
            return
        for i, name in enumerate(stars, 1):
            info = tools[name]
            aliases = ", ".join(a for a in info.get("aliases", []) if a != name)
            alias_str = f"  ({aliases})" if aliases else ""
            print(f"  {c('neon_pink', '★')} {c('neon', name)}{c('dim', alias_str)}")
        for name in orphan:
            print(f"  {c('yellow', '★')} {name}  {c('dim', '(not in registry)')}")
        print()
        print(c("dim", "  swe hs star <name>   toggle  ·  swe hs star clear   remove all\n"))
        return

    if len(args) > 1:
        print(c("red", f"  Unexpected args: {' '.join(args[1:])}"))
        return 1

    if args[0] in ("clear", "--clear"):
        from swe.harness.stars import save_stars

        save_stars([])
        print(f"  {c('green', '✓')} Cleared all stars.")
        return

    if args[0] in ("list", "ls"):
        return cmd_star([])

    key = args[0]
    name = resolve(tools, key)
    if not name:
        # A star can outlive its registry entry. Removing one used to need
        # `swe unstar`, because that command fell back to the raw name
        # while this one did not; the toggle now covers it.
        if is_starred(key):
            name = key
        else:
            print(c("red", f"  Tool '{key}' not found. Try 'swe list'."), file=err)
            return 1

    now_starred = toggle_star(name)
    if json_view:
        return emit(StateReply(name, _registry_state(name)).to_json())
    if now_starred:
        print(f"  {c('neon_pink', '★')} Starred {c('neon', name)} — pinned to top of {c('cyan', 'swe list')}")
    else:
        print(f"  {c('dim', '☆')} Unstarred {name}")


def cmd_info(args):
    if not args:
        print(c("red", "Usage: swe info <name|alias>"))
        return 1
    if len(args) > 1:
        print(c("red", f"  Unexpected args: {' '.join(args[1:])}"))
        return 1
    tools = load_registry()
    name = resolve(tools, args[0])
    if not name:
        print(c("red", f"  Tool '{args[0]}' not found. Try 'swe list'."))
        return 1

    info = tools[name]
    command = info.get("command") or ""
    installed = bool(command) and is_installed(command)
    path = (shutil.which(command) or "not found") if command else "not found"
    aliases = [a for a in info.get("aliases", []) if a != name]

    print(f"\n  {c('bold', name)}")

    # Two-column FIELD | VALUE table.
    # VALUE uses ``preformatted`` + ``trust_cell_width=True`` because
    # the value cell is sometimes ANSI-coloured (green/red for Status)
    # and sometimes variable-width (paths, multi-line descriptions).
    # ``fit="content"`` lets long paths / descriptions expand beyond
    # the column width rather than truncate — matches the old
    # ``print(f"  {label:<16} {val}")`` behaviour where overflow
    # scrolled rather than got cut.
    table = Table()
    table.add_column("label", "FIELD", width=16, kind="text")
    # VALUE holds free text (descriptions, notes, paths), so it takes what
    # the window leaves rather than a fixed 64.
    # 16 for FIELD, 3 for the " │ " gap, 2 for the indent the rows carry.
    _value_w = fit_widths(fixed=16 + 2, flex={"value": 64}, gap=3)["value"]
    table.add_column(
        "value", "VALUE", width=_value_w, max_width=_value_w,
        kind="preformatted", trust_cell_width=True,
    )

    rows = [
        ("Command:", command or "—"),
        ("Aliases:", ", ".join(aliases) if aliases else "—"),
        ("Description:", info.get("description") or "—"),
        ("Version:", info.get("version") or "unknown"),
        ("Tags:", ", ".join(info.get("tags", [])) or "—"),
        (
            "Status:",
            c("green", "installed") if installed else c("red", "not installed"),
        ),
        ("Path:", path),
    ]
    # A shelved harness still reports "installed" when its binary resolves,
    # which read as swe list hiding an installed tool for no reason. Say
    # when the row is archived (and why) so the two commands agree.
    state = state_of(info)
    if state != "active":
        meta = info.get("archived")
        detail = ""
        if isinstance(meta, dict):
            detail = " · ".join(
                part for part in
                (_fmt_when(meta.get("archived_at") or ""), meta.get("reason") or "")
                if part)
        rows.insert(6, ("State:", state + (f"  ·  {detail}" if detail else "")))
    colour = primary_color(name, info)
    if colour:
        rows.append(("Colour:", colour))
    if display_name(info):
        # Only when set, so a harness without one prints exactly as before.
        rows.insert(0, ("Display name:", display_name(info)))
    for label, val in rows:
        # trust_cell_width means the table will not truncate this column,
        # so a long description has to be trimmed before it goes in. Only
        # plain values: the coloured ones are short and eliding them would
        # cut an escape sequence in half.
        if isinstance(val, str) and "\x1b" not in val:
            val = elide(val, _value_w)
        table.add_row({"label": label, "value": val})
    if info.get("notes"):
        # ``Notes:`` is conditional — matches the pre-migration
        # behaviour where the optional row was only appended when
        # ``info["notes"]`` was truthy.
        table.add_row({"label": "Notes:", "value": elide(info["notes"], _value_w)})

    for line in table.render():
        print(line)
    print()


def _prompt_field(label: str, default: str = "") -> str:
    """Prompt for one field; Enter keeps the default (shown dim in brackets)."""
    hint = f" {c('dim', '[' + default + ']')}" if default else ""
    try:
        return read_line(c("cyan", f"  {label}>{hint} "))
    except (EOFError, KeyboardInterrupt):
        print()
        raise


def _ask(label: str, default: str) -> str:
    """Prompt for a field; an empty answer keeps the default."""
    raw = _prompt_field(label, default)
    val = raw.strip()
    return val if val else default


def _add_interactive(args: list[str]) -> int:
    """Interactive form: walk each field, show a summary, confirm before saving."""
    tools = load_registry()

    # -i/--interactive were already pulled out by cmd_add before flag
    # expansion, so the tokens here can only be real values or positionals.
    rest = args
    name_pre = command_pre = desc_pre = ""
    aliases_pre = ""
    tags_pre = "agentic, coding"
    positional: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--aliases" and i + 1 < len(rest):
            aliases_pre = rest[i + 1]; i += 2; continue
        if a == "--tags" and i + 1 < len(rest):
            tags_pre = rest[i + 1]; i += 2; continue
        if a == "--description" and i + 1 < len(rest):
            desc_pre = rest[i + 1]; i += 2; continue
        if a == "--command" and i + 1 < len(rest):
            command_pre = rest[i + 1]; i += 2; continue
        if not a.startswith("--"):
            positional.append(a)
        i += 1
    if positional:
        name_pre = positional[0]
    if len(positional) > 1:
        command_pre = positional[1]
    if len(positional) > 2:
        desc_pre = positional[2]

    draft = {
        "name": name_pre,
        "command": command_pre,
        "description": desc_pre,
        "aliases": aliases_pre,
        "tags": tags_pre,
    }

    print(c("bold", "\n  swe add — interactive"))
    print(c("dim", "  Walk through each field. Enter keeps the [default]. Ctrl-C to cancel.\n"))

    while True:
        try:
            # --- name (required, collision-checked) ---
            draft["name"] = _ask("name", draft["name"])
            while not draft["name"]:
                print(c("red", "  name is required"))
                draft["name"] = _ask("name", draft["name"])
            if not _ADD_NAME.fullmatch(draft["name"]):
                print(c("red", "  names start with a letter or digit; then . _ - too"))
                draft["name"] = ""
                continue
            owner = resolve(tools, draft["name"])
            if owner and owner != draft["name"]:
                print(c("yellow", f"  ⚠ '{draft['name']}' is an alias of '{owner}' — choose a different name."))
                continue
            if draft["name"] in tools:
                print(c("yellow", f"  ⚠ '{draft['name']}' already exists — saving will update it."))

            # --- command (required) ---
            draft["command"] = _ask("command", draft["command"])
            while not draft["command"]:
                print(c("red", "  command is required"))
                draft["command"] = _ask("command", draft["command"])

            # --- optional fields ---
            draft["description"] = _ask("description", draft["description"])
            draft["aliases"] = _ask("aliases", draft["aliases"])
            draft["tags"] = _ask("tags", draft["tags"])
        except (EOFError, KeyboardInterrupt):
            print(c("dim", "  Cancelled."))
            return 1

        # alias collision check
        aliases_list = _split_csv(draft["aliases"])
        collision = _alias_collision(tools, draft["name"], aliases_list)
        if collision:
            print(c("yellow", f"  ⚠ {collision}"))
            print(c("dim", "  Re-enter aliases (Enter to keep, or type new ones)."))
            try:
                draft["aliases"] = _ask("aliases", draft["aliases"])
            except (EOFError, KeyboardInterrupt):
                print(c("dim", "  Cancelled."))
                return 1
            aliases_list = _split_csv(draft["aliases"])
            if _alias_collision(tools, draft["name"], aliases_list):
                print(c("yellow", f"  ⚠ {collision} — not saved."))
                print(c("dim", "  Restarting field walk…\n"))
                continue

        # --- summary ---
        tags_list = _split_csv(draft["tags"])
        installed = is_installed(draft["command"])
        version = live_version(draft["command"]) if installed else None
        status_label = c("green", "installed") if installed else c("yellow", "not in PATH")
        print()
        print(c("bold", "  Summary"))
        print(f"    name:        {draft['name']}")
        print(f"    command:     {draft['command']}  ({status_label})")
        print(f"    description: {draft['description'] or c('dim', '—')}")
        print(f"    aliases:     {_format_list(aliases_list)}")
        print(f"    tags:        {_format_list(tags_list)}")
        if version:
            print(f"    version:     {c('dim', version)}  {c('dim', '(auto-detected)')}")
        print()

        # --- confirm ---
        try:
            choice = _prompt_field("Save? [Y]es / [e]dit / [c]ancel", "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(c("dim", "  Cancelled."))
            return 1
        if choice in ("", "y", "yes"):
            action = "Updated" if draft["name"] in tools else "Added"
            previous = tools.get(draft["name"]) or {}
            tools[draft["name"]] = {
                "command": draft["command"],
                "description": draft["description"],
                "version": version,
                "tags": tags_list,
                "aliases": aliases_list,
                "added": previous.get("added") or datetime.now().isoformat(),
                **_kept_fields(previous),
            }
            save_registry(tools)
            saved_status = c("green", "installed") if installed else c("yellow", "not yet in PATH")
            alias_str = f"  aliases: {', '.join(aliases_list)}" if aliases_list else ""
            print(f"  {c('green', '✓')} {action} '{draft['name']}' → '{draft['command']}' ({saved_status}){alias_str}")
            return 0
        if choice in ("e", "edit"):
            print(c("dim", "  Re-editing fields…\n"))
            continue
        print(c("dim", "  Cancelled."))
        return 1


# A registered harness name: starts alphanumeric, then these too.
_ADD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class HarnessRefusal(ValueError):
    """A registry write ``swe add`` or ``swe edit`` refused; nothing was
    written. The message says why, in the words the CLI prints."""


class AliasCollision(HarnessRefusal):
    """An alias another harness already answers to."""


class AlreadyRegistered(HarnessRefusal):
    """``swe add --no-replace`` for a name the registry holds."""


def cmd_add(args):
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    # -i detection must run on the raw argv: a standalone -i can only be the
    # boolean flag now that long values require =, while an expanded
    # --description=-i would smuggle -i in as a value.
    interactive = "-i" in args or "--interactive" in args
    args = [a for a in args if a not in ("-i", "--interactive")]
    try:
        args = expand_value_flags(
            args, {"--aliases", "--tags", "--description", "--command"}
        )
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1
    if interactive:
        # The form prompts and asks before overwriting; neither flag means
        # anything there, so refuse rather than quietly ignore them.
        if json_view or "--no-replace" in args:
            print(c("red", "  -i can't be combined with --json or --no-replace."), file=refusal)
            return 1
        return _add_interactive(args)
    if len(args) < 2:
        print(c("red", "Usage: swe add <name> <command> [description] [--aliases=a,b] [--tags=t1,t2]"),
              file=refusal)
        print(c("dim", "  Interactive: swe add -i   ·   swe add <name> -i"), file=refusal)
        return 1
    tools = load_registry()
    name = args[0]
    command = args[1]
    # None means "not supplied": re-adding keeps the saved description,
    # while an explicit `--description=` or empty positional clears it.
    desc: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    no_replace = False

    i = 2
    while i < len(args):
        if args[i] == "--aliases":
            aliases = _split_csv(args[i + 1])
            i += 2
        elif args[i] == "--tags":
            tags = _split_csv(args[i + 1])
            i += 2
        elif args[i] == "--description":
            desc = args[i + 1]
            i += 2
        elif args[i] == "--no-replace":
            no_replace = True
            i += 1
        elif args[i] == "--command":
            print(c("red", "  --command only prefills swe add -i; the command is a positional arg here."),
                  file=refusal)
            return 1
        elif args[i].startswith("-"):
            print(c("red", f"  Unknown flag: {args[i]}"), file=refusal)
            return 1
        else:
            desc = args[i]
            i += 1
    if command.startswith("-"):
        print(c("red", "Usage: swe add <name> <command> [description] [--aliases=a,b] [--tags=t1,t2]"),
              file=refusal)
        return 1
    try:
        reply, entry = _add_entry(tools, name, command, desc, aliases, tags,
                                  no_replace=no_replace)
    except HarnessRefusal as exc:
        print(c("red", f"  {exc}"), file=refusal)
        return 1
    if json_view:
        return emit(reply)
    action = reply["action"].capitalize()
    installed = reply["installed"]
    saved_aliases = reply["aliases"]
    status = c("green", "installed") if installed else c("yellow", "not yet in PATH")
    alias_str = f"  aliases: {', '.join(saved_aliases)}" if saved_aliases else ""
    print(f"  {c('green', '✓')} {action} '{name}' → '{command}' ({status}){alias_str}")
    if entry.get("state") == "archived":
        print(c("dim", "  still archived — `swe hs star` brings it back"))


def add_harness(name: str, command: str, description: str | None = None,
                aliases: list[str] | None = None, tags: list[str] | None = None,
                *, no_replace: bool = False) -> dict:
    """``swe add <name> <command> … --json`` in process: the reply it prints.

    ``None`` for a field means "not supplied", as on the command line: a
    re-add keeps the saved value, and a new entry gets the default
    (``tags`` is ``agentic, coding``). Raises ``HarnessRefusal`` for a bad
    name, a name already registered under ``no_replace``, or an alias
    another harness answers to; nothing is written then.
    """
    return _add_entry(load_registry(), name, command, description, aliases, tags,
                      no_replace=no_replace)[0]


def _add_entry(tools: dict, name: str, command: str, desc, aliases, tags,
               *, no_replace: bool) -> tuple[dict, dict]:
    """Write ``name`` into ``tools`` and save it: (the --json reply, the entry)."""
    if not _ADD_NAME.fullmatch(name):
        raise HarnessRefusal(f"{name!r} isn't a valid harness name: letters, digits,"
                             " then . _ - too")
    # Same cleaning `swe edit` applies: no blanks, no repeats. add_harness
    # callers hand lists straight in, so the CLI's parse is not enough.
    if aliases is not None:
        aliases = _normalize_field_value("aliases", aliases)
    if tags is not None:
        tags = _normalize_field_value("tags", tags)
    if no_replace and resolve(tools, name):
        raise AlreadyRegistered(f"{name} is already registered")
    # An alias another harness answers to would redirect `swe use` and
    # `swe remove` for that name.
    collision = _alias_collision(tools, name, aliases or [])
    if collision:
        raise AliasCollision(collision)

    action = "updated" if name in tools else "added"
    previous = tools.get(name) or {}
    tools[name] = {
        "command": command,
        "description": desc if desc is not None else previous.get("description", ""),
        # The stored version belongs to the probed binary: pointing the
        # entry at a different command must not keep the old one.
        "version": previous.get("version") if previous.get("command") == command else None,
        "tags": tags if tags is not None else previous.get("tags", ["agentic", "coding"]),
        "aliases": aliases if aliases is not None else previous.get("aliases", []),
        "added": previous.get("added") or datetime.now().isoformat(),
        **_kept_fields(previous),
    }
    save_registry(tools)
    entry = tools[name]
    reply = {"name": name, "command": command, "action": action,
             "installed": is_installed(command),
             "aliases": entry["aliases"], "tags": entry["tags"]}
    return reply, entry


def cmd_remove(args):
    if not args:
        print(c("red", "Usage: swe remove <name|alias>"))
        return 1
    if len(args) > 1:
        print(c("red", f"  Unexpected args: {' '.join(args[1:])}"))
        return 1
    tools = load_registry()
    name = resolve(tools, args[0])
    if not name:
        print(c("red", f"  Tool '{args[0]}' not found."))
        return 1
    del tools[name]
    save_registry(tools)
    print(f"  {c('green', '✓')} Removed '{name}' from registry.")


def cmd_use(args, env: dict | None = None):
    provider = None
    # Only before the name: everything after it belongs to the harness.
    if args and args[0].startswith("--provider="):
        provider = args[0].split("=", 1)[1]
        args = args[1:]
    if not args:
        print(c("red", "Usage: swe use [--provider=<slug>] <name|alias> "
                       "[extra args...]"))
        cmd_list([])
        return 1
    tools = load_registry()
    name = resolve(tools, args[0])
    extra = args[1:]
    if not name:
        print(c("red", f"  Tool '{args[0]}' not found. Try 'swe list'."))
        return 1
    # A state-only registry row (no command recorded) runs under its name,
    # as everywhere else that reads the registry.
    command = tools[name].get("command") or name
    if not is_installed(command):
        print(c("red", f"  Command '{command}' not found in PATH."))
        return 1
    if provider:
        env = _provider_env(name, provider, tools, env)
        if env is None:
            return 1
        label = f"{command} {' '.join(extra)}".strip()
        print(c("dim", f"  → {label}  (provider: {provider})\n"))
        os.execvpe(command, [command] + extra, env)
        return 0  # only reached when execvpe is mocked
    label = f"{command} {' '.join(extra)}".strip()
    print(c("dim", f"  → {label}\n"))
    if env:
        os.execvpe(command, [command] + extra, {**os.environ, **env})
    else:
        os.execvp(command, [command] + extra)


def _provider_env(name: str, provider: str, tools: dict,
                  extra_env: dict | None) -> dict | None:
    """The full environment for ``swe use --provider``: this process's,
    minus every keys-file variable, plus only ``provider``'s (see
    ``launch_provider``). None after printing why it can't (the note
    names variables, never values)."""
    from swe.remote import launch_provider
    from swe.remote.provider_exec import build_env

    try:
        path = launch_provider.keys_path()
        keys = launch_provider.read_keys(path)
        note = launch_provider.provider_error(name, provider, tools, keys)
        if note:
            raise ValueError(note)
        spec = (launch_provider.capability(name, tools) or {})[provider]
        env = build_env(dict(os.environ), spec, keys,
                        launch_provider.assigned_names(path))
    except ValueError as exc:
        print(c("red", f"  {exc}"))
        return None
    return {**env, **(extra_env or {})}


def cmd_check(args):
    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1
    from swe.harness.path_health import find_off_path_tools, preferred_npm_bin

    # Widths for the two cells that ship self-coloured ANSI via the
    # `preformatted`+`trust_cell_width=True` path; the names matter
    # because they're used by ``cpad`` AND by ``table.add_column`` so
    # the rendered cell and the schema header agree on column width.
    STATUS_COL_WIDTH = 2  # "✓ " or "✗ " (1 glyph + 1 trailing space)
    INFO_COL_WIDTH = 24   # "version unknown" + headroom

    tools = load_registry()
    if not tools:
        print(f"\n  {c('dim', 'No harnesses registered yet, nothing to check.')}")
        print(f"  {c('dim', 'Run')} {c('cyan', 'swe discover --apply')} "
              f"{c('dim', 'to register what is installed here.')}\n")
        return 0
    # Archived rows are listed for completeness but marked, and kept out of
    # the installed count: the summary's denominator is the active set, the
    # same scope `swe list` reports as "N/M installed". Otherwise a shelved
    # harness reads as a broken install and the two commands disagree.
    archived_set = set(archived_names(tools))
    updated = False
    n_installed = 0
    n_active = 0
    off_path_notes: list[str] = []
    print(f"\n{c('bold', 'Checking AI tools...')}\n")

    table = Table()
    table.add_column("status", "", width=STATUS_COL_WIDTH,
                     kind="preformatted", trust_cell_width=True)
    table.add_column("name", "NAME", width=22, kind="text")
    table.add_column("aliases", "ALIASES", width=18,
                     kind="list", color="cyan", empty="—")
    table.add_column("info", "INFO", width=INFO_COL_WIDTH,
                     kind="preformatted", trust_cell_width=True)

    on_path = _path_memo()
    versions = _live_versions(
        {info.get("command") or "" for info in tools.values()} - {""}, on_path)
    for name, info in sorted(tools.items()):
        aliases = [a for a in info.get("aliases", []) if a != name]
        command = info.get("command") or ""
        shelved = name in archived_set
        installed = bool(command) and on_path(command)
        if not shelved:
            n_active += 1
            n_installed += installed
        if installed:
            # Probe live version + heal stored version BEFORE we render
            # the row (this is the check-and-heal flow's side-effect,
            # not just a display step). An archived harness heals the
            # same way; only its rendered cells differ.
            ver = versions.get(command)
            if not ver:
                # Fall back to sanitizing whatever is already stored
                ver = extract_version_number(str(info.get("version") or ""))
            stored = info.get("version")
            if ver and ver != stored:
                tools[name]["version"] = ver
                updated = True
            elif not ver and stored:
                # Drop dirty banners/errors that aren't bare version numbers
                if extract_version_number(str(stored)) != stored:
                    tools[name]["version"] = None
                    updated = True
            mark, colour, note = "✓", "green", tools[name].get("version") or "version unknown"
        else:
            mark, colour, note = "✗", "red", "not installed"
        if shelved:
            mark, colour, note = "▪", "dim", "archived"
        table.add_row({
            "status": cpad(colour, mark, STATUS_COL_WIDTH),
            "name": name,
            "aliases": aliases,
            "info": cpad("dim", note, INFO_COL_WIDTH),
        })

    for line in table.render():
        print(line)

    print()
    summary = f"  {n_installed}/{n_active} installed"
    if archived_set:
        summary += f"  ·  {len(archived_set)} archived"
    print(c("dim", summary))

    # A shelved harness off PATH is not an accident to fix — skip it. The
    # scan still reports it under swe list --scope=archived.
    orphans = [o for o in find_off_path_tools(tools) if o[0] not in archived_set]
    if orphans:
        # Off-PATH diagnostic block lives BELOW the table — these
        # are not table rows (each orphan has a multi-line fix
        # recipe, so they don't fit the grid). Keeping them as
        # plain strings preserves the original output structure.
        print(f"\n{c('yellow', 'Off-PATH installs detected')} {c('dim', '(installed but invisible to swe)')}\n")
        for name, command, hit in orphans:
            print(f"  {c('yellow', '!')}  {name:<16} found at {c('dim', hit.path)}")
            print(c("dim", f"      source: {hit.source}  ·  not on current PATH"))
            print(c("dim", f"      fix: swe install {name}   # lists its installers"))
            print(c("dim", f"      or:  swe edit {name} --command={hit.path}"))
            off_path_notes.append(name)
        print()

    if updated:
        save_registry(tools)
        print(c("dim", "  Registry updated."))
    if off_path_notes:
        print(c("dim", f"  Tip: run {c('cyan', 'swe doctor')} for full Node/PATH diagnosis."))
    print()


def cmd_tags(args):
    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1
    tools = load_registry()
    tag_map: dict[str, list[str]] = {}
    for name, info in tools.items():
        for tag in info.get("tags", []):
            tag_map.setdefault(tag, []).append(name)

    if not tag_map:
        # No tags in registry at all — render a one-line notice
        # rather than an empty table (preserves the old behaviour
        # of just printing the title and trailing newline).
        print(f"\n{c('bold', 'Available tags')}\n")
        print(c("dim", "  No tags found.\n"))
        return

    print(f"\n{c('bold', 'Available tags')}\n")

    # Two-column TAG | TOOLS table. The TAG cell is ``preformatted``
    # + ``trust_cell_width=True`` so the cyan colour from cpad()
    # isn't double-wrapped by the Table renderer; the TOOLS cell
    # uses the list kind so multi-tool lists join with ``, `` and
    # adapt to fit mode.
    table = Table()
    table.add_column(
        "tag", "TAG", width=14,
        kind="preformatted", trust_cell_width=True,
    )
    table.add_column(
        "tools", "TOOLS", width=40,
        kind="list", color="dim", empty="—",
    )

    for tag in sorted(tag_map):
        table.add_row({
            "tag": cpad("cyan", tag, 14),
            "tools": sorted(tag_map[tag]),
        })

    for line in table.render():
        print(line)
    print()


def cmd_aliases(args):
    if args:
        print(c("red", f"  Unexpected args: {' '.join(args)}"))
        return 1
    tools = load_registry()
    print(f"\n{c('bold', 'Short aliases')}\n")

    # Two-column ALIASES | NAME table; the ``column_gap=" → "`` makes
    # the arrow separator part of the table so every row gets the
    # same horizontal alignment structurally (not visually padded).
    table = Table(column_gap=" → ")
    table.add_column(
        "aliases", "ALIASES", width=14,
        kind="list", color="cyan", empty="—",
    )
    table.add_column("name", "NAME", width=20, kind="text", fit="content")

    for name, info in sorted(tools.items()):
        aliases = [a for a in info.get("aliases", []) if a != name]
        if aliases:
            table.add_row({"aliases": aliases, "name": name})

    for line in table.render():
        print(line)
    print()


def doctor_report(home: Path | None = None) -> DoctorReport:
    """The ``swe doctor --json`` report, built in process (the web app's
    Doctor page reads it from here)."""
    return _doctor_probe(home or Path.home()).report


def _doctor_probe(home: Path) -> SimpleNamespace:
    """Every probe ``swe doctor`` runs, and the report made from them."""
    from swe.harness.doctor import (
        collect_findings,
        issue_findings,
        mcp_findings,
        mirror_audit,
        mirror_findings,
        plugin_findings,
        setup_findings,
    )
    from swe.harness.drift import run_drift_checks
    from swe.harness.path_health import (
        find_off_path_tools,
        nvm_bin_dirs,
        preferred_npm_bin,
        probe_node_env,
    )

    probe = SimpleNamespace(tools=load_registry(), env=probe_node_env())
    probe.nvm_dirs = nvm_bin_dirs(home)
    probe.preferred = preferred_npm_bin()
    probe.which_npm = shutil.which("npm")
    probe.orphans = find_off_path_tools(probe.tools)
    probe.drift = run_drift_checks(home=home)
    probe.setup = setup_findings(home)
    probe.issues = issue_findings(home)
    # Plugin drift needs the hub: before it exists, setup says so first.
    probe.plugins = plugin_findings(home) if not any(
        f.check in ("setup.hub", "setup.agents-md") for f in probe.setup) else []
    probe.mcp = mcp_findings(home)
    probe.mirror_audit = mirror_audit(home)
    probe.mirror = mirror_findings(probe.mirror_audit)
    probe.report = DoctorReport(
        collect_findings(probe.env, probe.preferred, probe.which_npm,
                         probe.nvm_dirs, probe.orphans, probe.drift)
        + probe.setup + probe.plugins + probe.mcp + probe.issues + probe.mirror)
    return probe


def cmd_doctor(args):
    """Diagnose Node/PATH mismatches that hide globally installed harnesses."""
    args, json_view = take_json_flag(list(args or []))
    fix_args = [a for a in args if a == "--fix" or a.startswith(_FIX_FLAGS)
                or a in ("--dry-run", "--check")]
    if fix_args and any(a == "--fix" or a.startswith("--fix=") for a in fix_args):
        rest = [a for a in args if a not in fix_args]
        if not rest:
            return _doctor_fix(Path.home(), fix_args, json_view)
        args = rest
    if args:
        message = f"  Unexpected args: {' '.join(args)}"
        if json_view:
            # stdout is one JSON document or nothing.
            print(message, file=sys.stderr)
        else:
            print(c("red", message))
        return 1
    from swe.harness.path_health import is_dir_on_path

    home = Path.home()
    probe = _doctor_probe(home)
    tools, env, nvm_dirs = probe.tools, probe.env, probe.nvm_dirs
    preferred, which_npm = probe.preferred, probe.which_npm
    orphans, drift, setup, report = probe.orphans, probe.drift, probe.setup, probe.report
    failed = not report.ok

    if json_view:
        emit(report.to_json())
        return 1 if failed else 0

    print(f"\n{c('bold', 'swe doctor')} — environment health\n")

    print(c("bold", "  Node / npm"))
    print(f"    node:         {env.node or c('red', 'not found')}"
          + (f"  {c('dim', '(' + env.node_version + ')')}" if env.node_version else ""))
    print(f"    npm:          {env.npm or c('red', 'not found')}"
          + (f"  {c('dim', '(' + env.npm_version + ')')}" if env.npm_version else ""))
    if env.global_prefix:
        on = c("green", "on PATH") if env.global_bin_on_path else c("red", "NOT on PATH")
        print(f"    global prefix:{' ' if True else ''} {env.global_prefix}")
        print(f"    global bin:    {env.global_bin or '—'}  ({on})")
    if preferred and which_npm and Path(preferred).resolve() != Path(which_npm).resolve():
        print(c("yellow", f"    note: which npm → {which_npm}; swe prefers {preferred}"))
    print()

    print(c("bold", "  nvm"))
    nvm_root = Path(os.environ.get("NVM_DIR", home / ".nvm"))
    if not nvm_root.exists():
        print(c("dim", "    not installed (or NVM_DIR unset)"))
    else:
        print(f"    NVM_DIR:      {nvm_root}")
        print(f"    node bins:    {len(nvm_dirs)} version(s)")
        on_path_nvm = [d for d in nvm_dirs if is_dir_on_path(d)]
        if on_path_nvm:
            print(c("green", f"    on PATH:      {on_path_nvm[0]}"))
        else:
            print(c("yellow", "    on PATH:      none — nvm globals are invisible to swe/non-interactive shells"))
            if nvm_dirs:
                print(c("dim", f"    latest bin:   {nvm_dirs[-1]}"))
    print()

    print(c("bold", "  Registry tools"))
    n_inst = sum(1 for i in tools.values() if is_installed(i.get("command", "")))
    n_arch = len(archived_names(tools))
    print(f"    registered:   {len(tools)}"
          + (f"  {c('dim', '(' + str(n_arch) + ' archived)')}" if n_arch else ""))
    print(f"    on PATH:      {c('green', str(n_inst))}/{len(tools)}")
    if orphans:
        print(f"    off-PATH:     {c('yellow', str(len(orphans)))}")
        print()
        print(c("yellow", "  Off-PATH installs (found on disk, not on PATH)"))
        for name, command, hit in orphans:
            print(f"    {c('yellow', '!')} {name}  ({command})")
            print(c("dim", f"        {hit.path}  [{hit.source}]"))
            print(c("dim", f"        fix: swe install {name}"))
            print(c("dim", f"         or: swe edit {name} --command={hit.path}"))
    else:
        print(c("dim", "    off-PATH:     none detected"))
    print()

    print(c("bold", "  Advice"))
    if not env.npm:
        print(c("red", "    • Install Node/npm, or put npm on PATH."))
    elif env.global_bin and not env.global_bin_on_path:
        print(c("yellow", f"    • Add npm global bin to PATH: export PATH=\"{env.global_bin}:$PATH\""))
    if nvm_dirs and not any(is_dir_on_path(d) for d in nvm_dirs):
        print(c("yellow", "    • Avoid `npm install -g` under nvm unless nvm is always on PATH."))
        print(c("dim", "      Prefer: swe install <name>   (uses PATH-visible Homebrew npm when available)"))
    if orphans:
        print(c("yellow", "    • Reinstall off-PATH tools with: swe install <name>"))
    if not orphans and env.global_bin_on_path:
        print(c("green", "    • Environment looks healthy for swe."))
    print()

    print(c("bold", "  Drift"))
    if not drift:
        print(c("dim", "    none detected"))
    else:
        for finding in drift:
            icon, color = ("✗", "red") if finding.severity == "error" else ("!", "yellow")
            print(f"    {c(color, icon)} [{finding.area}] {finding.message}")
    print()

    if setup:
        print(c("bold", "  Setup"))
        for finding in setup:
            print(f"    {c('yellow', '!')} {finding.message}")
            if finding.fix:
                print(c("dim", f"        fix: {finding.fix}"))
        print()

    for title, rows in (("Plugins", probe.plugins), ("MCP servers", probe.mcp)):
        if not rows:
            continue
        print(c("bold", f"  {title}"))
        for finding in rows:
            print(f"    {c('yellow', '!')} {finding.message}")
            if finding.fix:
                print(c("dim", f"        fix: {finding.fix}"))
        print()

    from swe.harness.doctor import mirror_lines

    print(c("bold", "  Transcript mirror"))
    for label, value in mirror_lines(probe.mirror_audit):
        print(f"    {label + ':':<14}{value}")
    for finding in probe.mirror:
        icon, color = ("!", "yellow") if finding.severity == "warning" else ("•", "dim")
        print(f"    {c(color, icon)} {finding.message}")
        if finding.fix:
            print(c("dim", f"        fix: {finding.fix}"))
    print()

    if probe.issues:
        print(c("bold", "  Issues"))
        for finding in probe.issues:
            icon, color = ("!", "yellow") if finding.severity == "warning" else ("•", "dim")
            print(f"    {c(color, icon)} {finding.message}")
            if finding.fix:
                print(c("dim", f"        fix: {finding.fix}"))
        print()
    print(c("dim", "  Related: swe check  ·  swe install <name>  ·  swe help doctor\n"))

    return 1 if failed else 0


_FIX_FLAGS = ("--fix=", "--message=", "--action=")


def _doctor_fix(home: Path, args: list, json_view: bool) -> int:
    """``swe doctor --fix[=<check>] [--message=<text> [--action=<a>]]
    [--dry-run]``: run the fixes, read the report again, say what changed.

    Exit 0 when every fix that ran worked and no finding in scope has a
    batch fix left; the findings that need a person do not count.
    """
    from swe.harness import doctor_fix

    def value(flag: str) -> str | None:
        hits = [a.split("=", 1)[1] for a in args if a.startswith(flag + "=")]
        return hits[-1] if hits else None

    check, message, action = value("--fix"), value("--message"), value("--action")
    dry_run = "--dry-run" in args or "--check" in args
    out = error_stream(json_view)
    if (message is not None or action is not None) and not check:
        print("  --message and --action name one finding: add --fix=<check>", file=out)
        return 1
    if action is not None and message is None:
        print("  --action runs one finding's fix: add --message=<text>", file=out)
        return 1
    try:
        result = doctor_fix.run_fixes(
            home, lambda: doctor_report(home).to_json()["findings"],
            check=check, message=message, action=action, dry_run=dry_run)
    except doctor_fix.FixRefused as exc:
        print(f"  {exc}", file=out)
        return 1
    failed = any(step["error"] for step in result["ran"])
    left = doctor_fix.fixable_left(result)
    code = 0 if dry_run or not (failed or left) else 1
    if json_view:
        emit(result)
        return code

    verb = "Would run" if dry_run else "Ran"
    print(f"\n{c('bold', 'swe doctor --fix')}{' (dry run)' if dry_run else ''}\n")
    if not result["ran"]:
        print(c("dim", "  Nothing to fix here."))
    for step in result["ran"]:
        icon = (c("cyan", "·") if dry_run else
                c("red", "✗") if step["error"] else c("green", "✓"))
        what = step["fix"] or step["action"]
        print(f"  {icon} {step['action']:<9} {what}")
        for finding in step["findings"]:
            print(c("dim", f"      {finding['check']}: {finding['message']}"))
        if step["error"]:
            for line in str(step["error"]).splitlines()[:8]:
                print(c("red", f"      {line}"))
    if result["not_cleared"]:
        print()
        print(c("bold", "  Ran, but still there (needs you)"))
        for finding in result["not_cleared"]:
            print(f"    {c('yellow', '!')} {finding['check']}: {finding['message']}")
    if result["skipped"]:
        print()
        print(c("bold", "  Needs a click per finding"))
        for finding in result["skipped"]:
            print(f"    {c('yellow', '!')} {finding['check']}: {finding['message']}")
            print(c("dim", f"        runs: {', '.join(finding['actions'])}"))
    print()
    if dry_run:
        print(c("dim", f"  {verb} {len(result['ran'])} fix(es). A later fix may find more "
                       "once an earlier one has run."))
    else:
        manual = len(result["still_failing"]) - len(left)
        print(f"  Fixed {c('green', str(len(result['fixed'])))}"
              f" · still fixable {c('red' if left else 'dim', str(len(left)))}"
              f" · need you {manual}")
        if manual:
            print(c("dim", "  Run swe doctor to see what is left and how to fix it."))
    print()
    return code


def _install_command_name(reg_name: str, tools: dict) -> str:
    """The binary a harness provides: registry entry, else catalog, else name."""
    from swe.harness.catalog import HARNESS_CATALOG

    entry = tools.get(reg_name) or {}
    catalog = HARNESS_CATALOG.get(reg_name, {})
    return entry.get("command") or catalog.get("command") or reg_name


def _install_method_available(via: str) -> bool:
    """Whether the tool a method needs is on PATH right now."""
    from swe.harness.installers import METHOD_TOOL

    return shutil.which(METHOD_TOOL[via]) is not None


def _install_json(names: list[str], tools: dict) -> dict:
    """The ``swe install --json`` document for the given registry names."""
    from swe.harness.catalog import HARNESS_CATALOG
    from swe.harness.installers import (
        METHOD_TOOL,
        WEBSITES,
        installed_app,
        installed_cask,
        installers_for,
        uninstaller_for,
    )

    harnesses = []
    for name in names:
        command = _install_command_name(name, tools)
        binary = shutil.which(command)
        # GUI apps never land on PATH. For a provider that is only the app
        # (no catalog command) the bundle or its Caskroom record is the
        # install evidence; for a CLI-bearing one they only say the app
        # exists — `installed` keeps meaning the listed command resolves,
        # so the sheet still offers the CLI's own methods.
        app = installed_app(name)
        caskroom = installed_cask(name)
        path = binary
        if path is None and name not in HARNESS_CATALOG:
            path = app or caskroom
        # The remover follows the executable's own real home first; the
        # Caskroom names a cask only when no PATH executable claimed it.
        remover = uninstaller_for(
            name, os.path.realpath(binary or caskroom)
        ) if (binary or caskroom) else None
        harnesses.append({
            "name": name,
            "command": command,
            "installed": path is not None,
            "path": path,
            "app": app,
            "description": HARNESS_CATALOG.get(name, {}).get("description", ""),
            "website": WEBSITES.get(name),
            "registered": name in tools,
            "methods": [{
                "via": inst.via,
                "package": inst.package,
                "command": inst.display(),
                "available": _install_method_available(inst.via),
            } for inst in installers_for(name)],
            # How to remove it, read off where the binary resolves to; null
            # when it isn't installed or a vendor script put it there.
            "uninstall": None if remover is None else {
                "via": remover.via,
                "package": remover.package,
                "command": remover.display(),
                "available": shutil.which(METHOD_TOOL[remover.via]) is not None,
            },
        })
    return {"harnesses": harnesses}


def cmd_install(args):
    """Show or run a harness's install command per package manager."""
    from swe.harness.catalog import HARNESS_CATALOG
    from swe.harness.installers import (
        INSTALLERS,
        METHODS,
        METHOD_TOOL,
        WEBSITES,
        Installer,
        installed_app,
        installed_cask,
        installers_for,
    )

    args, json_view = take_json_flag(list(args or []))
    err = error_stream(json_view)

    try:
        args = expand_value_flags(args, {"--via", "--package", "--command"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1

    name = None
    via = None
    package = None
    command = None
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--via":
            via = args[i + 1]
            i += 2
        elif args[i] == "--package":
            package = args[i + 1]
            i += 2
        elif args[i] == "--command":
            command = args[i + 1]
            i += 2
        elif args[i] in ("--dry-run", "-n"):
            dry_run = True
            i += 1
        elif args[i].startswith("-") and args[i] != "-":
            print(c("red", f"Unknown argument: {args[i]}"), file=err)
            return 1
        elif name is None:
            name = args[i]
            i += 1
        else:
            print(c("red", f"Unexpected argument: {args[i]}"), file=err)
            return 1

    if json_view and (via or package or command or dry_run):
        print(c("red", "--json cannot be combined with --via, --package, --command or --dry-run"),
              file=err)
        return 1

    if name is None:
        if json_view:
            # A provider with only a website still earns a row: the sheet
            # links out instead of dead-ending on an empty methods list.
            names = sorted(set(INSTALLERS) | set(WEBSITES))
            return emit(_install_json(names, load_registry_readonly()))
        print(c("red", "Usage: swe install <name> [--via=<method>] [--package=<pkg>] "
                       "[--command=<cmd>] [--dry-run|-n] [--json]"))
        print(c("dim", "  Example: swe install gemini            # list its installers"))
        print(c("dim", "           swe install gemini --via=npm  # run one"))
        print(c("dim", "           swe install mastracode --package=mastracode"))
        return 1

    # Read-only: only the path that actually installs may create harness.json.
    tools = load_registry_readonly()
    # Allow installing by alias, but a harness's own id wins: another
    # harness may list that id among its aliases, and the Doctor's fix
    # names the id.
    resolved = name if name in tools else resolve(tools, name)
    reg_name = resolved or name

    # A scoped @owner/pkg name is an explicit npm package; its registry name
    # is the last segment.
    scoped = name.startswith("@") and "/" in name
    if scoped and not package:
        package = name
        reg_name = name.split("/")[-1]

    catalog = HARNESS_CATALOG.get(reg_name, {})
    # The registry wins over the catalog. An entry already on disk has been
    # through discovery and possibly hand-editing on this machine, so the
    # catalog's generic line is the fallback for names never seen here, not
    # an upgrade over what is already recorded.
    entry = tools.get(reg_name) or {}
    cmd_name = command or entry.get("command") or catalog.get("command") or reg_name
    installers = installers_for(reg_name)

    if json_view:
        if not installers:
            print(c("red", f"  No known installer for '{name}'. "
                           "If it is on npm, pass --package=<npm-name>."), file=err)
            return 1
        return emit(_install_json([reg_name], tools))

    installer = None
    if via is not None:
        if via not in METHODS:
            print(c("red", f"  Unknown method '{via}'. Methods: {', '.join(METHODS)}."),
                  file=err)
            return 1
        if package and via != "npm":
            print(c("red", f"  --package only applies to npm installs (--via=npm)."),
                  file=err)
            return 1
        if package:
            # --package already names the npm package; the table entry is moot.
            installer = Installer("npm", package)
        else:
            known = {inst.via: inst for inst in installers}
            if via not in known:
                if installers:
                    have = ", ".join(inst.via for inst in installers)
                    print(c("red", f"  '{reg_name}' has no {via} installer. "
                                   f"Try: {have}."), file=err)
                else:
                    print(c("red", f"  No known installer for '{reg_name}'. "
                                   "If it is on npm, pass --package=<npm-name>."), file=err)
                return 1
            installer = known[via]
    elif package:
        installer = Installer("npm", package)
    else:
        # Listing: print, never run. npm (or any tool) may be absent.
        if not installers:
            site = WEBSITES.get(reg_name)
            print(c("red", f"  No known installer for '{name}'. "
                           "If it is on npm, pass --package=<npm-name>."), file=err)
            if site:
                print(c("dim", f"  Or get it from {site}"), file=err)
            return 1
        print(f"\n{c('bold', 'swe install')}\n")
        print(f"  name:     {reg_name}")
        print(f"  command:  {cmd_name}")
        found = shutil.which(cmd_name)
        if found:
            print(c("green", f"  already installed at {found}"))
            print(c("dim", "  hint: 'swe discover --apply' registers it"))
        else:
            app = installed_app(reg_name) or installed_cask(reg_name)
            if app:
                # For a provider that is only the app this is the install;
                # for a CLI-bearing one it is just the desktop app sitting
                # next to a missing command.
                if reg_name in HARNESS_CATALOG:
                    print(c("green", f"  its app is at {app}"))
                else:
                    print(c("green", f"  already installed at {app}"))
        print()
        for inst in installers:
            row = f"  {inst.via:<7} {inst.display()}"
            if _install_method_available(inst.via):
                print(row)
            else:
                print(c("dim", f"{row}  ({METHOD_TOOL[inst.via]} not on PATH)"))
        print()
        print(c("dim", f"  Run one: swe install {reg_name} --via=<method>"))
        return 0

    try:
        npm, argv = install_argv(installer)
    except InstallError as exc:
        print(c("red", f"  {exc}"), file=err)
        return 1
    shown = " ".join(argv) if installer.via == "npm" else installer.display()

    print(f"\n{c('bold', 'swe install')}\n")
    print(f"  name:     {reg_name}")
    print(f"  via:      {installer.via}")
    print(f"  package:  {installer.package}")
    print(f"  command:  {cmd_name}")
    print()

    if dry_run:
        print(c("dim", f"  dry-run: would run  {shown}"))
        return 0

    print(c("dim", f"  → {shown}\n"))
    result = run_install(reg_name, cmd_name, installer, argv, npm)
    if result.action is None:
        print(c("red", f"  {result.error}"))
        if result.exited and installer.via == "npm":
            print(c("dim", "  Tip: if the package name differs, try --package=<npm-name>"))
        return result.code

    if not result.path:
        print(c("yellow", f"  install finished, but '{cmd_name}' is still not on PATH."))
        print(c("dim", "  Run: swe doctor"))
    elif shutil.which(cmd_name):
        print(f"  {c('green', '✓')} on PATH: {result.path}")
    else:
        # A GUI app install: its evidence is the bundle, not a binary.
        print(f"  {c('green', '✓')} installed at {result.path}")
    status = c("green", "installed") if result.path else c("yellow", "registered (not on PATH yet)")
    print(f"  {c('green', '✓')} {result.action} '{reg_name}' → '{cmd_name}' ({status})"
          + (f"  v{result.version}" if result.version else ""))
    print(c("dim", f"  Try: swe info {reg_name}  ·  swe use {reg_name}\n"))
    return result.code


class InstallError(Exception):
    """An install that can't start: an unknown method, or its tool off PATH."""


class InstallResult(SimpleNamespace):
    """What ``run_install`` did. ``action`` is "Added" or "Updated" once the
    install ran and the registry was written, None when it failed (``error``
    says why). ``code`` is ``swe install``'s exit status: 0 only when the
    command is on PATH afterwards. ``output`` is the tail of what the
    installer printed, when it was captured. ``exited`` is False when the
    installer never finished (a timeout, or it couldn't start)."""


def install_argv(installer) -> tuple[str | None, list[str]]:
    """The resolved npm (npm only) and the argv ``installer`` runs.
    Raises InstallError when the tool it needs is not on PATH."""
    from swe.harness.installers import METHOD_TOOL
    from swe.harness.path_health import preferred_npm_bin

    npm = preferred_npm_bin() if installer.via == "npm" else None
    if installer.via == "npm" and not npm:
        raise InstallError("No npm found on PATH. Install Node or fix PATH first (see swe doctor).")
    tool = METHOD_TOOL[installer.via]
    if installer.via != "npm" and not shutil.which(tool):
        raise InstallError(f"'{tool}' is needed for --via={installer.via} but is not on PATH.")
    return npm, installer.argv(npm or "npm")


# How much of a captured installer's output a caller gets back.
INSTALL_OUTPUT_TAIL = 4000


def _tail(log) -> str:
    """The last INSTALL_OUTPUT_TAIL bytes of an open log file, as text."""
    size = log.seek(0, os.SEEK_END)
    log.seek(max(0, size - INSTALL_OUTPUT_TAIL))
    return log.read().decode("utf-8", errors="replace")


def run_install(reg_name: str, cmd_name: str, installer, argv: list[str],
                npm: str | None, *, capture: bool = False) -> InstallResult:
    """Run ``argv`` (from ``install_argv``) and register the harness: the
    body of ``swe install --via``. ``capture`` collects the installer's
    output with nothing on stdin, for a caller with no terminal (a cask or
    script that asks for a password then fails instead of waiting)."""
    from swe.harness.catalog import HARNESS_CATALOG
    from swe.harness.tools import live_version

    # Installing writes the registry afterwards, so this is where the
    # creating load belongs: listing and --json never reach it.
    tools = load_registry()
    catalog = HARNESS_CATALOG.get(reg_name, {})
    entry = tools.get(reg_name) or {}

    timeout = 300 if installer.via == "npm" else 600
    if capture:
        # A file rather than a pipe: an installer that prints for ten
        # minutes costs disk, not hub memory, and what it printed before a
        # timeout is still there to show.
        with tempfile.TemporaryFile() as log:
            try:
                result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                        stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return InstallResult(code=1, action=None, path=None, version=None, output=_tail(log),
                                     exited=False, error=f"{installer.via} install timed out.")
            except Exception as exc:
                return InstallResult(code=1, action=None, path=None, version=None, output=_tail(log),
                                     exited=False, error=f"{installer.via} install failed: {exc}")
            output = _tail(log)
    else:
        output = ""
        try:
            result = subprocess.run(
                argv,
                capture_output=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return InstallResult(code=1, action=None, path=None, version=None, output=output,
                                 exited=False, error=f"{installer.via} install timed out.")
        except Exception as exc:
            return InstallResult(code=1, action=None, path=None, version=None, output=output,
                                 exited=False, error=f"{installer.via} install failed: {exc}")

    if result.returncode != 0:
        return InstallResult(code=result.returncode, action=None, path=None, version=None,
                             output=output, exited=True,
                             error=f"{installer.via} install exited with code {result.returncode}")

    # Re-hash PATH resolution. A command still off PATH is registered all
    # the same, so it can be fixed later. A GUI app (a cask install) leaves
    # a bundle or a Caskroom record instead of a binary.
    installed_path = shutil.which(cmd_name)
    ver = live_version(cmd_name) if installed_path else None
    if not installed_path:
        from swe.harness.installers import installed_app, installed_cask
        installed_path = installed_app(reg_name) or installed_cask(reg_name)

    # Fallback: read package.json next to npm global module
    if not ver and installer.via == "npm":
        try:
            import json as _json

            prefix = subprocess.run(
                [npm, "prefix", "-g"],
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            if installer.package.startswith("@"):
                scope, pname = installer.package.split("/", 1)
                pkg_json = Path(prefix) / "lib" / "node_modules" / scope / pname / "package.json"
            else:
                pkg_json = Path(prefix) / "lib" / "node_modules" / installer.package / "package.json"
            if pkg_json.is_file():
                raw_ver = _json.loads(pkg_json.read_text()).get("version") or ""
                ver = extract_version_number(raw_ver) or raw_ver or None
        except Exception:
            pass

    desc = entry.get("description") or catalog.get("description") or ""
    tags = list(entry.get("tags") or catalog.get("tags") or ["agentic", "coding"])
    aliases = list(entry.get("aliases") or catalog.get("aliases") or [])
    colour = entry.get("primary_color") or catalog.get("primary_color")

    previous = tools.get(reg_name, {})
    entry = {
        **previous,
        "command": cmd_name,
        "description": desc,
        "version": ver or (
            previous.get("version")
            if previous.get("command") == cmd_name else None
        ),
        "tags": tags,
        "aliases": aliases,
    }
    if colour:
        entry["primary_color"] = colour
    if reg_name in tools:
        action = "Updated"
    else:
        entry["added"] = datetime.now().isoformat()
        action = "Added"

    tools[reg_name] = entry
    save_registry(tools)
    return InstallResult(code=0 if installed_path else 1, action=action, path=installed_path,
                         version=ver, output=output, exited=True, error=None)


def install_harness(name: str, via: str) -> InstallResult:
    """``swe install <name> --via=<via>`` with no terminal: the harness's
    own installer for that method, its output captured. Raises
    InstallError when there is none or its tool is off PATH. ``name`` is
    the harness's own id, never read as an alias: one harness may list
    another's id among its aliases, and the button names the id."""
    from swe.harness.installers import installers_for

    tools = load_registry_readonly()
    reg_name = name
    installer = next((inst for inst in installers_for(reg_name) if inst.via == via), None)
    if installer is None:
        raise InstallError(f"'{reg_name}' has no {via} installer.")
    cmd_name = _install_command_name(reg_name, tools)
    npm, argv = install_argv(installer)
    return run_install(reg_name, cmd_name, installer, argv, npm, capture=True)


EDITABLE_FIELDS = (
    "command", "display_name", "description", "aliases", "tags", "version",
    "notes", "primary_color",
)


def _flag_name(field: str) -> str:
    """How a field is spelled on the command line: ``display_name`` is ``--display-name``."""
    return field.replace("_", "-")


def _field_from_flag(name: str) -> str:
    """The field a flag word (hyphen or underscore, no dashes) names, or ""."""
    field = name.replace("-", "_")
    return field if field in EDITABLE_FIELDS else ""


def _split_csv(value: str) -> list[str]:
    if value is None:
        return []
    items = [part.strip() for part in str(value).split(",")]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _format_list(values) -> str:
    if not values:
        return "—"
    return ", ".join(values)


def _show_edit_fields(name: str, info: dict) -> None:
    print(f"\n  {c('bold', name)}")
    rows = [
        ("command", info.get("command", "")),
        ("display name", display_name(info) or "—"),
        ("description", info.get("description") or "—"),
        ("aliases", _format_list([a for a in info.get("aliases", []) if a != name])),
        ("tags", _format_list(info.get("tags", []))),
        ("version", info.get("version") or "—"),
        ("notes", info.get("notes") or "—"),
        ("colour", primary_color(name, info) or "—"),
    ]
    for label, val in rows:
        print(f"  {'  ' + label + ':':<16} {val}")
    print()


def _parse_set_string(raw: str) -> dict:
    """Parse ``field=value`` pairs; list values may contain commas.

    Example: ``tags=agentic,coding,notes=hi`` → tags='agentic,coding', notes='hi'
    """
    # A field is written as stored (display_name) or as its flag (display-name).
    words = "|".join(sorted(
        {w for f in EDITABLE_FIELDS for w in (f, _flag_name(f))}, key=len, reverse=True))
    pattern = re.compile(r"(?:^|,)\s*(" + words + r")=")
    matches = list(pattern.finditer(raw))
    if not matches:
        raise ValueError(f"Invalid --set value '{raw}' (expected field=value)")
    updates: dict = {}
    for idx, match in enumerate(matches):
        field = _field_from_flag(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        updates[field] = raw[start:end].strip().rstrip(",")
    return updates


_COLOUR_HINT = "Use a colour like #D97757."


def _normalise_colour(value) -> str | None:
    """``#d97757`` or ``d97757`` -> ``#D97757``; blank clears the field."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    hex_part = text[1:] if text.startswith("#") else text
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_part):
        raise ValueError(_COLOUR_HINT)
    return "#" + hex_part.upper()


def _parse_edit_flags(args: list[str]) -> tuple[dict, list[str]]:
    """Return (updates, remaining_positional_args)."""
    args = expand_value_flags(
        args,
        {f"--{word}" for f in EDITABLE_FIELDS for word in (f, _flag_name(f))}
        | {"--set"},
    )
    updates: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--set":
            updates.update(_parse_set_string(args[i + 1]))
            i += 2
            continue
        if arg.startswith("--") and _field_from_flag(arg[2:]):
            updates[_field_from_flag(arg[2:])] = args[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            raise ValueError(f"Unknown flag: {arg}")
        rest.append(arg)
        i += 1
    return updates, rest


def _normalize_field_value(field: str, value):
    if field in ("aliases", "tags"):
        if isinstance(value, list):
            return _split_csv(",".join(str(v) for v in value))
        return _split_csv(value if value is not None else "")
    if field == "version":
        text = "" if value is None else str(value).strip()
        return text or None
    if field in ("notes", "display_name"):
        text = "" if value is None else str(value).strip()
        return text or None
    if field == "primary_color":
        return _normalise_colour(value)
    return "" if value is None else str(value).strip()


def _alias_collision(tools: dict, name: str, aliases: list[str]) -> str | None:
    mapping = {}
    for other, info in tools.items():
        if other == name:
            continue
        mapping[other] = other
        for alias in info.get("aliases", []):
            mapping[alias] = other
    for alias in aliases:
        if alias == name:
            continue
        owner = mapping.get(alias)
        if owner:
            return f"Alias '{alias}' already used by '{owner}'"
    return None


def _apply_edits(tools: dict, name: str, updates: dict) -> tuple[dict, list[str]]:
    """Apply updates to a copy of the entry. Returns (new_info, change_lines)."""
    info = dict(tools[name])
    changes: list[str] = []

    for field, raw in updates.items():
        if field not in EDITABLE_FIELDS:
            raise HarnessRefusal(f"Unknown field '{field}'")
        new_val = _normalize_field_value(field, raw)
        old_val = info.get(field)
        if field in ("aliases", "tags"):
            old_norm = list(old_val or [])
            if field == "aliases":
                old_norm = [a for a in old_norm if a != name]
            if old_norm == new_val:
                continue
            if field == "aliases":
                conflict = _alias_collision(tools, name, new_val)
                if conflict:
                    raise AliasCollision(conflict)
            info[field] = new_val
            changes.append(f"{field}: {_format_list(old_norm)} → {_format_list(new_val)}")
            continue

        if field == "command":
            if not new_val:
                raise HarnessRefusal("command cannot be empty")
            old_disp = old_val or "—"
            if old_val == new_val:
                continue
            info[field] = new_val
            changes.append(f"command: {old_disp} → {new_val}")
            continue

        old_disp = old_val if old_val not in (None, "") else "—"
        new_disp = new_val if new_val not in (None, "") else "—"
        if old_val == new_val or (old_val in (None, "") and new_val in (None, "")):
            continue
        if new_val in (None, ""):
            info.pop(field, None)
            if field in ("description",):
                info[field] = ""
        else:
            info[field] = new_val
        changes.append(f"{field}: {old_disp} → {new_disp}")

    return info, changes


def _edit_interactive(name: str, info: dict, tools: dict) -> dict | None:
    """Interactive field editor. Returns updates dict, or None if cancelled."""
    draft = dict(info)
    updates: dict = {}
    print(c("dim", "  Edit fields: command, display name, description, aliases, tags, version, notes, primary color"))
    print(c("dim", "  Commands:  save  |  quit  |  show  |  <field>"))
    print(c("dim", "  Blank value keeps current. For lists, use commas (e.g. mc,ms).\n"))

    while True:
        try:
            choice = read_line(c("cyan", "  field> ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print(c("dim", "  Cancelled."))
            return None

        if not choice:
            continue
        if choice in ("quit", "q", "exit", "cancel"):
            print(c("dim", "  Cancelled."))
            return None
        if choice in ("show", "print", "fields"):
            # Merge pending updates for display
            preview = dict(draft)
            preview.update({k: _normalize_field_value(k, v) for k, v in updates.items()})
            _show_edit_fields(name, preview)
            continue
        if choice in ("save", "s", "done", "write"):
            if "aliases" in updates:
                new_aliases = _normalize_field_value("aliases", updates["aliases"])
                conflict = _alias_collision(tools, name, new_aliases)
                if conflict:
                    print(c("yellow", f"  ⚠ {conflict}"))
                    print(c("dim", "  Enter a different alias, then save again."))
                    continue
            return updates
        # "display name", "display-name" and "display_name" all pick the field.
        choice = _field_from_flag(choice.replace(" ", "_")) or choice
        if choice not in EDITABLE_FIELDS:
            print(c("red", f"  Unknown field/command: {choice}"))
            names = ", ".join(_flag_name(f).replace("-", " ") for f in EDITABLE_FIELDS)
            print(c("dim", f"  Fields: {names}"))
            continue

        current = draft.get(choice)
        if choice in ("aliases", "tags"):
            cur_list = list(current or [])
            if choice == "aliases":
                cur_list = [a for a in cur_list if a != name]
            current_disp = ", ".join(cur_list)
        else:
            current_disp = "" if current is None else str(current)

        label = _flag_name(choice).replace("-", " ")
        print(c("dim", f"  current {label}: {current_disp or '—'}"))
        try:
            new_raw = read_line(c("cyan", f"  new {label}> "))
        except (EOFError, KeyboardInterrupt):
            print()
            print(c("dim", "  Cancelled."))
            return None

        if new_raw.strip() == "" and new_raw != "":
            # whitespace-only treated as clear for text? keep simple: blank keep
            continue
        if new_raw == "":
            print(c("dim", "  (kept)"))
            continue

        try:
            normalized = _normalize_field_value(choice, new_raw)
        except ValueError as exc:
            print(c("red", f"  {exc}"))
            continue
        updates[choice] = new_raw
        draft[choice] = normalized if normalized is not None else ""
        if choice in ("aliases", "tags"):
            print(c("green", f"  set {label} = {_format_list(normalized)}"))
        else:
            print(c("green", f"  set {label} = {normalized if normalized not in (None, '') else '—'}"))


def _edit_harness_json(name: str, info: dict) -> dict:
    """The ``swe list --json`` row for one harness, as ``swe edit --json`` echoes it."""
    from swe.harness.archive import load_archive

    return HarnessRow.of(
        name, info, set(load_stars()), load_archive(), _session_counts(100), None
    ).to_json()


def edit_harness(key: str, updates: dict) -> EditReply:
    """``swe edit <key> --field=value… --json`` in process: the reply it prints.

    ``updates`` maps a field in ``EDITABLE_FIELDS`` to the text the flag
    would carry (lists comma-separated, or a list); an empty value clears
    the field. Raises ``LookupError`` for a name or alias the registry
    lacks, and ``HarnessRefusal`` (``AliasCollision`` for an alias another
    harness answers to) for an edit swe refuses. Nothing is written then,
    nor when nothing changed.
    """
    tools = load_registry()
    name = resolve(tools, key)
    if not name:
        raise LookupError(f"Tool '{key}' not found. Try 'swe list'.")
    new_info, changes = _apply_edits(tools, name, updates)
    if not changes:
        return EditReply(name, [], _edit_harness_json(name, tools[name]))
    # Built before the write: a failure here must not leave an edit saved
    # that the caller was told failed.
    harness = _edit_harness_json(name, new_info)
    tools[name] = new_info
    save_registry(tools)
    # Each change line reads "field: old -> new".
    return EditReply(name, [line.split(":", 1)[0] for line in changes], harness)


def cmd_edit(args):
    """Edit harness registry fields (flags or interactive)."""
    args, json_view = take_json_flag(list(args or []))
    try:
        return _edit(args, json_view)
    except ConfigurationError as exc:
        # The dispatcher does the same for every command; handled here too so
        # a direct call (and its tests) keeps --json stdout empty.
        print(c("red", str(exc)), file=error_stream(json_view))
        return 1


def _edit(args, json_view):
    # Under --json a refusal must leave stdout empty: the app decodes it whole.
    err = error_stream(json_view)

    if not args:
        print(c("red", "Usage: swe edit <name|alias> [--field=value ...]"), file=err)
        if not json_view:
            print(c("dim", "  Interactive: swe edit mastracode"))
            print(c("dim", "  Flags:       swe edit mastracode --description='...' --aliases=mc"))
        return 1

    try:
        updates, rest = _parse_edit_flags(args)
    except ValueError as exc:
        print(c("red", f"  {exc}"), file=err)
        return 1

    if not rest:
        print(c("red", "Usage: swe edit <name|alias> [--field=value ...]"), file=err)
        return 1

    key = rest[0]
    if rest[1:]:
        print(c("red", f"Unexpected arguments: {' '.join(rest[1:])}"), file=err)
        return 1

    if json_view and not updates:
        # The interactive editor reads the terminal; the app has none.
        print(c("red", "  swe edit --json needs at least one field flag "
                       f"({', '.join('--' + _flag_name(f) for f in EDITABLE_FIELDS)})"), file=err)
        return 1

    tools = load_registry()
    name = resolve(tools, key)
    if not name:
        print(c("red", f"  Tool '{key}' not found. Try 'swe list'."), file=err)
        return 1

    info = tools[name]
    if json_view:
        try:
            return emit(edit_harness(name, updates).to_json())
        except ValueError as exc:
            # A refusal, or a value the field can't take (a colour that isn't one).
            print(c("red", f"  {exc}"), file=err)
            return 1
    _show_edit_fields(name, info)

    if not updates:
        interactive = _edit_interactive(name, info, tools)
        if interactive is None:
            return 1
        updates = interactive

    if not updates:
        print(c("dim", "  No changes."))
        return 0

    try:
        new_info, changes = _apply_edits(tools, name, updates)
    except ValueError as exc:
        print(c("red", f"  {exc}"), file=err)
        return 1

    if not changes:
        print(c("dim", "  No changes."))
        return 0

    tools[name] = new_info
    save_registry(tools)
    print(f"  {c('green', '✓')} Updated '{name}'")
    for line in changes:
        print(c("dim", f"    · {line}"))
    print()
    return 0


def cmd_harness_usage(args):
    """``swe harness usage <name> --account-dir=<dir>``: one extra account's
    rate limit, read from that account's own files — never the default
    account's credentials or the shared rate-limit cache. Only Devin and
    Cursor have per-account files swe knows."""
    args, json_view = take_json_flag(list(args or []))
    err = error_stream(json_view)
    try:
        args = expand_value_flags(args, {"--account-dir"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1
    name = None
    account_dir = None
    i = 0
    while i < len(args):
        if args[i] == "--account-dir":
            account_dir = args[i + 1]
            i += 2
        elif args[i].startswith("-"):
            print(c("red", f"Unknown argument: {args[i]}"), file=err)
            return 1
        elif name is None:
            name = args[i]
            i += 1
        else:
            print(c("red", f"Unexpected argument: {args[i]}"), file=err)
            return 1
    usage = "Usage: swe harness usage <devin|cursor> --account-dir=<dir> [--json]"
    if name is None or account_dir is None:
        print(c("red", f"  {usage}"), file=err)
        return 1
    if not os.path.isdir(account_dir):
        print(c("red", f"  No such directory: {account_dir}"), file=err)
        return 1

    from swe.harness.rate_limits import get_account_usage
    from swe.harness.remedies import remedy_for, remedy_for_account

    try:
        # Fetch helpers may warn to stdout; under --json stdout is the
        # document, so their noise goes to stderr there.
        with (contextlib.redirect_stdout(sys.stderr) if json_view
              else contextlib.nullcontext()):
            outcome = get_account_usage(name, account_dir)
    except ValueError as exc:
        print(c("red", f"  {exc}"), file=err)
        return 1
    limit = outcome.limit
    # The account's files are what failed, so its remedy points at that
    # account's login, not the default one's.
    remedy = (remedy_for_account(name, outcome.status, account_dir)
              if account_dir else remedy_for(name, outcome.status))
    if json_view:
        # Every verdict is a typed document: ``status`` names the failure
        # and ``remedy`` the fix, so the app never collapses them into a
        # bare "couldn't fetch". Failures still exit 1 — the document
        # rides along, and readers from before the status field keep
        # seeing a failed call rather than a signed-out account.
        emit({
            "name": name,
            "account_dir": account_dir,
            "status": outcome.status,
            "remedy": remedy,
            "limit": limit.to_json() if limit is not None else None,
        })
        return 0 if outcome.status in ("ok", "signed-out", "no-sub") else 1
    if outcome.status == "signed-out":
        print(f"  {name}: not signed in")
        return 0
    if limit is None or outcome.status not in ("ok", "no-sub"):
        # A failure the remedy names: say what to do, not just that it
        # failed.
        if remedy is not None:
            print(c("yellow", f"  {remedy['summary']}."))
            for i, step in enumerate(remedy["steps"], 1):
                print(f"    {i}. {step}")
            if remedy.get("command"):
                print(c("dim", f"      {remedy['command']}"))
            if remedy.get("url"):
                print(c("dim", f"      open '{remedy['url']}'"))
        else:
            print(c("red", f"  {outcome.detail or outcome.status}"))
        return 1
    print(f"  {c('bold', name)}  {limit.format_column()}")
    from swe.harness.models import RateLimitInfo
    for w in limit.windows:
        # A window the provider left unread is unknown, not 0%: render it
        # the way the list renders a missing reading.
        if w.get("unavailable"):
            print(f"    {w['window']:>8}  {c('dim', '—')}")
            continue
        cell = RateLimitInfo(
            tool_name=name,
            used_percent=w["used_percent"],
            limit_reached=w["limit_reached"],
            reset_at=w["reset_at"],
            plan_type=limit.plan_type,
            window_seconds=w["window_seconds"],
            window=w["window"],
        ).format_column()
        print(f"    {w['window']:>8}  {cell}")
    return 0


def cmd_harness_usage_events(args):
    """``swe harness usage-events cursor [--account-dir=<dir>] [--since=<s>]``:
    Cursor's per-event usage feed — the editor, cursor-agent, cloud agents
    and Grok Bot — as rows the app ingests into its usage log."""
    args, json_view = take_json_flag(list(args or []))
    err = error_stream(json_view)
    try:
        args = expand_value_flags(args, {"--account-dir", "--since"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1
    name = None
    account_dir = None
    since = None
    i = 0
    while i < len(args):
        if args[i] == "--account-dir":
            account_dir = args[i + 1]
            i += 2
        elif args[i] == "--since":
            try:
                since = float(args[i + 1])
            except ValueError:
                print(c("red", f"  Bad --since: {args[i + 1]}"), file=err)
                return 1
            i += 2
        elif args[i].startswith("-"):
            print(c("red", f"Unknown argument: {args[i]}"), file=err)
            return 1
        elif name is None:
            name = args[i]
            i += 1
        else:
            print(c("red", f"Unexpected argument: {args[i]}"), file=err)
            return 1
    usage = ("Usage: swe harness usage-events cursor "
             "[--account-dir=<dir>] [--since=<epoch-seconds>] [--json]")
    if name != "cursor":
        print(c("red", f"  {usage}"), file=err)
        return 1
    if account_dir is not None and not os.path.isdir(account_dir):
        print(c("red", f"  No such directory: {account_dir}"), file=err)
        return 1

    from swe.harness.rate_limits import fetch_cursor_usage_events

    # Like `swe harness usage`: the account's state.vscdb alone, never the
    # default account's token or the keychain.
    state_db = (
        os.path.join(account_dir, "User", "globalStorage", "state.vscdb")
        if account_dir is not None else None
    )
    # Helper warnings may print to stdout; under --json stdout is the
    # document, so their noise goes to stderr there.
    with (contextlib.redirect_stdout(sys.stderr) if json_view
          else contextlib.nullcontext()):
        status, events, account_id = fetch_cursor_usage_events(
            state_db, since=since)
    if json_view:
        return emit({"status": status, "account_id": account_id,
                     "events": events})
    if status == "unavailable":
        print(c("red", "  Cursor's usage feed could not be read"), file=err)
        return 1
    if status == "signed-out":
        print("  cursor: not signed in")
        return 0
    if status == "auth-required":
        print("  cursor: session expired — sign in again")
        return 0
    if status == "incomplete":
        print(c("yellow", "  cursor: the feed hit its page cap on part of "
                "the window — these events may not be all of it"),
              file=err)
    total_tokens = sum(
        e["input_tokens"] + e["output_tokens"]
        + e["cache_read_tokens"] + e["cache_write_tokens"] for e in events)
    total_cost = sum(e["cost_usd"] for e in events if e["cost_usd"] is not None)
    print(f"  {c('bold', 'cursor')}  {len(events)} events, "
          f"{total_tokens:,} tokens, ${total_cost:.2f}")
    by_model: dict[str, tuple[int, int, float]] = {}
    for event in events:
        count, tokens, cost = by_model.get(event["model"], (0, 0, 0.0))
        by_model[event["model"]] = (
            count + 1,
            tokens + event["input_tokens"] + event["output_tokens"]
            + event["cache_read_tokens"] + event["cache_write_tokens"],
            cost + (event["cost_usd"] or 0.0),
        )
    for model, (count, tokens, cost) in sorted(
        by_model.items(), key=lambda item: -item[1][1]
    ):
        print(f"    {model:<32} {count:>6} events  {tokens:>14,} tokens  "
              f"${cost:.2f}")
    return 0


def cmd_harness_usage_daily(args):
    """``swe harness usage-daily codex [--account-dir=<dir>] [--json]``:
    one Codex account's per-day token usage, asked of ``codex
    app-server`` with ``CODEX_HOME`` at that account's home — never the
    default account's files. Codex is the only harness whose app-server
    answers this."""
    args, json_view = take_json_flag(list(args or []))
    err = error_stream(json_view)
    try:
        args = expand_value_flags(args, {"--account-dir"})
    except ValueError as exc:
        print(c("red", str(exc)), file=err)
        return 1
    name = None
    account_dir = None
    i = 0
    while i < len(args):
        if args[i] == "--account-dir":
            account_dir = args[i + 1]
            i += 2
        elif args[i].startswith("-"):
            print(c("red", f"Unknown argument: {args[i]}"), file=err)
            return 1
        elif name is None:
            name = args[i]
            i += 1
        else:
            print(c("red", f"Unexpected argument: {args[i]}"), file=err)
            return 1
    usage = ("Usage: swe harness usage-daily codex "
             "[--account-dir=<dir>] [--json]")
    if name != "codex":
        print(c("red", f"  {usage}"), file=err)
        return 1
    if account_dir is not None and not os.path.isdir(account_dir):
        print(c("red", f"  No such directory: {account_dir}"), file=err)
        return 1

    from swe.harness.codex_usage import (
        fetch_codex_usage_daily, print_codex_usage_daily)

    report = fetch_codex_usage_daily(account_dir)
    if json_view:
        return emit(report)
    return print_codex_usage_daily(report, account_dir)
