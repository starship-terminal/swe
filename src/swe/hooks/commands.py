"""``swe hooks`` — manage the hook scripts under ~/.swe/hooks.

Three tiers show up here. ``harness`` rows are the per-harness scripts
``swe init`` links into each hooks root. ``shared`` rows are the scripts
under ``hooks/shared/`` that ``swe hooks sync`` deploys through generated
wrappers (``hooks/shared.py`` documents the mechanism). ``foreign`` rows
are hooks the inventory sees but no ``~/.swe`` source claims — files
in a hooks root another installer owns, or a settings-declared command
that points outside the hub. ``list`` shows all three, ``check`` reports
where declarations and links disagree, ``info`` and ``edit`` work on one
script, ``new`` writes the skeleton plus the stanza to paste, ``delete``
removes the hub file (``--foreign`` the real file), ``disable`` /
``enable`` flip a hook without deleting it, ``sync`` regenerates the
wrappers and re-applies the link plan, and ``stats`` / ``log`` report what
the hooks did from the run log and the Claude transcripts
(``hooks/stats_commands.py``). The settings files are read-only
here — declaring stays with you, and deleting never touches a stanza.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from swe import paths as _paths
from swe.paths import home_relative as _short
from swe.console import c, sanitize, terminal_width
from swe.flags import expand_value_flags
from swe.init.hooks import apply_hook_status, plan_hooks
from swe.init.layout import (
    LinkIgnoreError,
    aliases_of,
    load_linkignore,
    load_registry,
)
from swe.json_output import emit, error_stream, take_json_flag
from swe.hooks import shared as _shared
from swe.hooks.contracts import contract_for
from swe.hooks.create import (
    command_for,
    create,
    create_shared,
    settings_stanza,
    settings_targets,
    shared_command_for,
)
from swe.hooks.inventory import (
    all_rows,
    declarations_by_harness,
    find_script,
    hook_findings,
    script_summary,
)
from swe.hooks.models import HookRow
from swe.table import Table

SUBCOMMANDS = ("list", "info", "edit", "new", "check", "delete",
               "disable", "enable", "sync", "stats", "log")

# Same vocabulary swe init prints; colour is what you would do about it.
_STATE_COLOR = {
    "linked": "green", "create": "cyan", "absorb": "cyan",
    "relink": "yellow", "conflict": "red", "keep": "yellow",
    "skipped": "dim", "ignored": "dim", "foreign": "yellow",
    "unlinked": "cyan",
}

_TIER_COLOR = {"shared": "cyan", "harness": "dim", "foreign": "yellow"}


def print_hooks_help() -> None:
    from swe.help_text import HELP

    summary, detail = HELP["hooks"]
    print(f"\n  {c('bold', 'swe hooks')} — {summary}\n")
    print(detail)
    print()


def cmd_hooks(args) -> int:
    args = list(args or [])
    if args and args[0] in ("-h", "--help", "help"):
        print_hooks_help()
        return 0
    args, json_view = take_json_flag(args)

    def fail(message: str) -> int:
        # --json promises stdout holds one document or nothing.
        print(message, file=error_stream(json_view))
        return 1

    # A leading flag means the default subcommand: `swe hooks --harness=x`
    # is `swe hooks list --harness=x`.
    if args and not args[0].startswith("-"):
        sub, rest = args[0], args[1:]
    else:
        sub, rest = "list", args
    if sub in ("list", "ls"):
        return _list(rest, json_view, fail)
    if sub == "info":
        return _info(rest, json_view, fail)
    if sub == "edit":
        return _edit(rest, fail)
    if sub == "new":
        return _new(rest, json_view, fail)
    if sub == "check":
        return _check(rest, json_view, fail)
    if sub in ("delete", "rm"):
        return _delete(rest, json_view, fail)
    if sub == "disable":
        return _toggle(rest, json_view, fail, off=True)
    if sub == "enable":
        return _toggle(rest, json_view, fail, off=False)
    if sub == "sync":
        return _sync(rest, json_view, fail)
    if sub in ("stats", "log"):
        from swe.hooks import stats_commands

        run = stats_commands.stats if sub == "stats" else stats_commands.log
        return run(rest, json_view, fail)
    return fail(c("red", f"Unknown subcommand: {sub}. "
                       f"Use one of {', '.join(SUBCOMMANDS)}."))


# --- shared plumbing --------------------------------------------------------


def _harness_arg(args: list[str]) -> tuple[list[str], str | None]:
    """Pull a ``--harness=<name>`` option out; positionals stay behind.

    ``expand_value_flags`` has already split ``--harness=x`` into the pair
    ``("--harness", "x")`` by the time this runs, so both spellings land
    here — the pair form wins, the compact form is kept for callers that
    skipped expansion.
    """
    rest: list[str] = []
    harness = None
    i = 0
    while i < len(args):
        if args[i] == "--harness" and i + 1 < len(args):
            harness = args[i + 1]
            i += 2
        elif args[i].startswith("--harness="):
            harness = args[i].split("=", 1)[1]
            i += 1
        else:
            rest.append(args[i])
            i += 1
    return rest, harness


def _resolve_harness(name: str, registry: dict, known: set[str]) -> str:
    """The registry key ``--harness=<name>`` means; aliases resolve too."""
    if name in known:
        return name
    matches = [
        key for key, entry in registry.items()
        if isinstance(entry, dict) and name in aliases_of(entry)
    ]
    return matches[0] if len(matches) == 1 else name


def _rows(home: Path) -> tuple[list[HookRow], dict]:
    """All rows joined with declarations; a bad .linkignore warns, not dies."""
    registry = load_registry(home)
    try:
        patterns = load_linkignore(home)
    except LinkIgnoreError as exc:
        print(f"warning: {exc}; showing link states without it",
              file=sys.stderr)
        patterns = []
    statuses = plan_hooks(home, patterns=patterns, registry=registry)
    declared = declarations_by_harness(home, registry)
    return all_rows(home, registry, statuses, declared), registry


def _row_json(r: HookRow, summaries: dict[Path, str]) -> dict:
    """The stable ``swe hooks list --json`` row shape the app page reads.

    ``summaries`` caches header lines by source for one listing: a shared
    script is a row per harness it deploys to, and is read once.
    """
    if r.source not in summaries:
        summaries[r.source] = (
            script_summary(r.source) if r.source.is_file() else "")
    return {
        "name": r.script,
        "harness": r.harness,
        "tier": r.tier,
        "source_path": str(r.source),
        "dest_path": str(r.dest),
        "link_status": r.state,
        "detail": r.detail,
        "enabled": r.enabled,
        "declared": [{
            "event": d.event,
            "matcher": d.matcher,
            "file": str(d.settings),
        } for d in r.declared],
        "events_supported": r.events_supported,
        # The header line the app shows beside the name; a directory hook
        # has no single file to read it from.
        "summary": summaries[r.source],
    }


def _declared_cell(row: HookRow) -> str:
    if not row.declared:
        return c("dim", "-")
    # sanitize strips our ANSI too, so it goes on the raw labels.
    labels = sorted({sanitize(d.label()) for d in row.declared})
    return ", ".join(labels)


def _state_cell(row: HookRow) -> str:
    colour = _STATE_COLOR.get(row.state, "dim")
    word = row.state
    if row.detail and row.state in ("skipped", "ignored", "relink", "conflict"):
        word += f" ({row.detail})"
    return c(colour, word)


# --- list -------------------------------------------------------------------


def _list(rest: list[str], json_view: bool, fail) -> int:
    try:
        rest = expand_value_flags(rest, {"--harness"})
    except ValueError as exc:
        return fail(c("red", str(exc)))
    rest, harness = _harness_arg(rest)
    if rest:
        return fail(c("red", f"Unexpected args: {' '.join(rest)}"))
    home = Path.home()
    rows, registry = _rows(home)
    if harness:
        harness = _resolve_harness(harness, registry, {r.harness for r in rows})
        rows = [r for r in rows if r.harness == harness]
        if not rows:
            return fail(c("red", f"No hook scripts under "
                                 f"~/.swe/hooks/{harness}/"))

    if json_view:
        summaries: dict[Path, str] = {}
        return emit({"hooks": [_row_json(r, summaries) for r in rows]})

    if not rows:
        print(c("dim", "\n  No hook scripts in ~/.swe/hooks yet."))
        print(c("dim", "  swe hooks new <harness> <name> writes a skeleton.\n"))
        return 0

    print(f"\n{c('bold', 'Hooks')}  "
          f"{c('dim', _short(_paths.hooks_dir_for(home), home))}\n")
    table = Table()
    table.add_column("script", "SCRIPT", width=10, max_width=34)
    table.add_column("harness", "HARNESS", width=7, max_width=14)
    table.add_column("tier", "TIER", width=4, max_width=8,
                     kind="preformatted")
    table.add_column("state", "LINK", width=5, max_width=44,
                     kind="preformatted")
    table.add_column("declared", "DECLARED", width=8, max_width=60)
    for r in rows:
        table.add_row({
            "script": sanitize(r.script),
            "harness": sanitize(r.harness),
            "tier": c(_TIER_COLOR.get(r.tier, "dim"), r.tier),
            "state": _state_cell(r),
            "declared": _declared_cell(r),
        })
    for line in table.render():
        print(f"  {line}")
    linked = sum(1 for r in rows if r.state == "linked")
    foreign = sum(1 for r in rows if r.tier == "foreign")
    summary = (f"{len(rows)} scripts · {linked} linked · {foreign} foreign · "
               "swe hooks check finds the gaps")
    print(f"\n  {c('dim', summary)}\n")
    return 0


# --- info -------------------------------------------------------------------


def _info(rest: list[str], json_view: bool, fail) -> int:
    try:
        rest = expand_value_flags(rest, {"--harness"})
    except ValueError as exc:
        return fail(c("red", str(exc)))
    rest, harness = _harness_arg(rest)
    if len(rest) != 1:
        return fail(c("red", "Usage: swe hooks info <name> [--harness=<h>]"))
    home = Path.home()
    rows, registry = _rows(home)
    matches = find_script(rest[0], rows)
    if harness:
        harness = _resolve_harness(harness, registry, {r.harness for r in rows})
        matches = [r for r in matches if r.harness == harness]
    if not matches:
        return fail(c("red", f"No hook script named {rest[0]} under "
                             "~/.swe/hooks."))

    if json_view:
        return emit({
            "script": rest[0],
            "harnesses": [{
                "harness": r.harness,
                "tier": r.tier,
                "enabled": r.enabled,
                "source_path": str(r.source),
                "dest_path": str(r.dest),
                "link_status": r.state,
                "detail": r.detail,
                "summary": script_summary(r.source),
                "events_supported": r.events_supported,
                "declared": [{
                    "event": d.event,
                    "matcher": d.matcher,
                    "timeout": d.timeout,
                    "command": d.command,
                    "file": str(d.settings),
                } for d in r.declared],
            } for r in matches],
        })

    print(f"\n{c('bold', sanitize(rest[0]))}\n")
    for r in matches:
        print(f"  {c('cyan', r.harness)}  "
              f"{c(_TIER_COLOR.get(r.tier, 'dim'), r.tier)}")
        print(f"    source: {_short(r.source, home)}")
        if not r.enabled:
            print(f"    {c('yellow', 'disabled — swe hooks enable restores')}")
        if r.source.is_dir():
            print(f"    {c('dim', 'a directory hook — scripts inside it')}")
        summary = script_summary(r.source) if r.source.is_file() else ""
        if summary:
            for line in _wrap(summary, terminal_width() - 12):
                print(f"    doc:    {c('dim', sanitize(line))}")
        dest = f"{_short(r.dest, home)}  {_state_cell(r)}"
        print(f"    dest:   {dest}")
        if r.declared:
            for d in r.declared:
                timeout = f", timeout {d.timeout:g}" if d.timeout else ""
                print(f"    decl:   {sanitize(d.label())}{c('dim', timeout)}"
                      f"  {c('dim', _short(d.settings, home))}")
        else:
            print(f"    decl:   {c('dim', 'not declared — the hook will not fire')}")
        print()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, max(20, width)) or [text]


# --- edit -------------------------------------------------------------------


def _editor_argv() -> list[str]:
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        return shlex.split(command) or ["vi"]
    except ValueError:
        return ["vi"]


def _edit_target(row: HookRow) -> Path | None:
    """The real file to hand the editor — never a symlinked path.

    The hub copy is the file: a destination link resolves to it anyway, and
    an editor that saves by rename would otherwise replace the link with a
    regular file and split the hook in two. A hub path that is itself a
    symlink is resolved through; one that stays a symlink or does not exist
    is refused.
    """
    target = row.source
    try:
        if target.is_symlink():
            target = target.resolve()
    except OSError:
        return None
    if target.is_symlink() or not target.is_file():
        return None
    return target


def _edit(rest: list[str], fail) -> int:
    try:
        rest = expand_value_flags(rest, {"--harness"})
    except ValueError as exc:
        return fail(c("red", str(exc)))
    rest, harness = _harness_arg(rest)
    if len(rest) != 1:
        return fail(c("red", "Usage: swe hooks edit <name> [--harness=<h>]"))
    name = rest[0]
    home = Path.home()
    rows, registry = _rows(home)
    matches = find_script(name, rows)
    if harness:
        harness = _resolve_harness(harness, registry, {r.harness for r in rows})
        matches = [r for r in matches if r.harness == harness]
    if not matches:
        return fail(c("red", f"No hook script named {name} under ~/.swe/hooks."))
    # Foreign rows are read-only everywhere; delete and toggle refuse
    # them explicitly, and edit does too — opening one would hand a file
    # swe does not own to the user's editor.
    matches = [r for r in matches if r.tier != "foreign"]
    if not matches:
        return fail(c("red", f"{name} is a foreign hook — swe displays "
                             "it read-only; edit the file directly."))
    # A shared script rows once per deploy target — same file each time.
    # One source, one edit target.
    unique: dict[Path, HookRow] = {}
    for r in matches:
        unique.setdefault(r.source, r)
    matches = list(unique.values())
    row = matches[0]
    if len(matches) > 1:
        if not sys.stdin.isatty():
            names = ", ".join(sorted({r.harness for r in matches}))
            return fail(c("red", f"{name} exists under {names} — "
                                 "which one? --harness=<name>"))
        print(f"\n  {name} exists under several harnesses:")
        for i, r in enumerate(matches, 1):
            print(f"    {i}. {r.harness}  {c('dim', _short(r.source, home))}")
        from swe.prompt import read_line

        try:
            choice = read_line("  Edit which? [1]: ").strip() or "1"
        except EOFError:
            print()
            return 1
        try:
            index = int(choice)
            if not 1 <= index <= len(matches):
                raise IndexError
            row = matches[index - 1]
        except (ValueError, IndexError):
            return fail(c("red", f"No such choice: {choice}"))

    if row.source.is_dir():
        return fail(c("red", f"{name} is a directory hook — "
                             f"edit files inside {row.source}"))
    target = _edit_target(row)
    if target is None:
        return fail(c("red", f"{name} resolves to no real file — "
                             "refusing to open a symlinked path."))
    if row.harness == "codex" and row.declared:
        # [hooks.state] in config.toml keys commands by trusted hash: an
        # edit that changes the command line invalidates trust and codex
        # re-prompts on next run. Editing the script body is safe; the
        # declared path is what is hashed.
        print(f"  {c('yellow', '!')} codex trusts declared commands by "
              f"hash ([hooks.state] in config.toml): keep the command "
              f"path unchanged or codex re-prompts\n")
    argv = _editor_argv() + [str(target)]
    try:
        done = subprocess.run(argv, check=False)
    except OSError as exc:
        return fail(c("red", f"Cannot run {argv[0]}: {exc}"))
    return int(done.returncode)


# --- new --------------------------------------------------------------------


def _new(rest: list[str], json_view: bool, fail) -> int:
    if len(rest) != 2:
        return fail(c("red",
                      "Usage: swe hooks new <harness|shared> <name>"))
    harness, name = rest
    home = Path.home()
    registry = load_registry(home)
    if harness == "shared":
        return _new_shared(name, home, registry, json_view, fail)
    # plan_hooks treats the folder name as a registry key: `new cc gate`
    # must land under claude/, not an orphaned cc/ that never links.
    harness = _resolve_harness(harness, registry, set(registry))
    try:
        target = create(harness, name, home)
    except ValueError as exc:
        return fail(c("red", str(exc)))
    command, note = command_for(harness, target.name, registry, home)
    stanza = settings_stanza(command, contract_for(registry, harness, home))
    targets = settings_targets(registry, harness, home)

    if json_view:
        return emit({
            "path": str(target),
            "harness": harness,
            "command": command,
            "settings": [str(t) for t in targets],
            "stanza": stanza,
        })

    print(f"\n  {c('green', '✓')} Created {_short(target, home)} (executable)\n")
    where = " or ".join(_short(t, home) for t in targets) \
        if targets else f"~/.{harness}/settings.json"
    print(f"  Declare it yourself — swe never writes a settings file.")
    print(f"  Paste into {c('cyan', where)} (merge with events already there):\n")
    for line in stanza.splitlines():
        print(f"  {c('dim', line)}")
    print()
    if note:
        print(f"  {c('yellow', '!')} {note}")
    print(f"  {c('dim', 'Then swe init links it into place; swe hooks list shows both halves.')}\n")
    return 0


def _new_shared(
    name: str, home: Path, registry: dict, json_view: bool, fail
) -> int:
    """``swe hooks new shared <name>``: the normalized-contract skeleton."""
    try:
        target = create_shared(name, home)
    except ValueError as exc:
        return fail(c("red", str(exc)))
    # deploy_targets covers every shared script — keep only this one's.
    targets = [t for t in _shared.deploy_targets(home, registry)
               if t.source == target]
    commands = {
        t.harness: shared_command_for(t.harness, t.source.name,
                                      registry, home)
        for t in targets
    }
    stanza = None
    stanza_harness = None
    for harness_name in sorted(commands):
        command = commands[harness_name]
        if command:
            # The stanza must speak this harness's own contract: gemini's
            # BeforeTool and millisecond timeout are not claude's
            # PreToolUse and 10 seconds.
            stanza = settings_stanza(
                command, contract_for(registry, harness_name, home))
            stanza_harness = harness_name
            break

    if json_view:
        return emit({
            "path": str(target),
            "harness": "shared",
            "tier": "shared",
            "commands": commands,
            "deploys_to": sorted(commands),
            "stanza": stanza,
        })

    print(f"\n  {c('green', '✓')} Created {_short(target, home)} (executable)\n")
    print(f"  Edit its {c('cyan', '# events: [...]')} header — that is what "
          f"deploys it. Then:")
    print(f"    1. {c('bold', 'swe hooks sync')} generates the wrappers")
    if targets:
        print(f"    2. declare the wrapper in each harness's hooks map:")
        for t in targets:
            command = commands.get(t.harness)
            if command:
                print(f"       {c('cyan', t.harness)}: {command}")
    else:
        dormant = ("no installed shim-able harness covers "
                   "the declared events yet")
        print(f"    2. {c('dim', dormant)}")
    print()
    if stanza:
        schema = contract_for(registry, stanza_harness, home).schema
        print(f"  {c('cyan', stanza_harness)} stanza ({schema} schema — "
              f"merge into its settings file):\n")
        for line in stanza.splitlines():
            print(f"  {c('dim', line)}")
        print()
    return 0


# --- check ------------------------------------------------------------------


def _check(rest: list[str], json_view: bool, fail) -> int:
    if rest:
        return fail(c("red", f"Unexpected args: {' '.join(rest)}"))
    home = Path.home()
    try:
        patterns = load_linkignore(home)
    except LinkIgnoreError as exc:
        print(f"warning: {exc}; checking without it", file=sys.stderr)
        patterns = []
    findings = hook_findings(home, patterns=patterns)
    if json_view:
        emit({
            "findings": [
                {"kind": f.kind, "harness": f.harness, "message": f.message}
                for f in findings
            ]
        })
        return 1 if any(f.kind != "managed" for f in findings) else 0
    print(f"\n{c('bold', 'Hook drift')}\n")
    if not findings:
        print(f"  {c('green', '✓')} declared, linked and present all agree\n")
        return 0
    real = [f for f in findings if f.kind != "managed"]
    notes = [f for f in findings if f.kind == "managed"]
    for f in real:
        print(f"  {c('yellow', '!')} [{f.harness}] {sanitize(f.message)}")
    for f in notes:
        print(f"  {c('dim', '·')} [{f.harness}] {sanitize(f.message)}")
    if real:
        note = (f"{len(real)} findings — swe init fixes the link "
                "side; declarations stay yours to edit")
        print(f"\n  {c('dim', note)}\n")
        return 1
    print()
    return 0


# --- delete / disable / enable / sync -----------------------------------------


def _flag(rest: list[str], name: str) -> tuple[list[str], bool]:
    """Pull one boolean ``--name`` out of argv; True when present."""
    return [a for a in rest if a != name], name in rest


def _confirm(question: str, assume_yes: bool) -> bool:
    """Delete-style confirmation: --yes, or an interactive y."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    from swe.prompt import read_line

    try:
        answer = read_line(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _delete_row_files(rows: list[HookRow], home: Path) -> list[str]:
    """Remove what ``rows`` owns; one outcome line per path.

    Shared rows repeat one source across deploy targets and foreign rows
    can name the same file from two harnesses, so sources are deduped —
    the second unlink would only report a false failure. A removal that
    fails says ``failed`` and the caller exits nonzero: deleting half a
    hook while claiming success would leave live wrappers pointing at a
    source that is still there.
    """
    done: list[str] = []
    seen: set[Path] = set()
    for row in rows:
        if row.tier == "foreign":
            # --foreign deletes the real file, nothing else.
            target = row.source
            if target in seen:
                continue
            seen.add(target)
            if target.is_dir() and not target.is_symlink():
                done.append(f"skipped {target} (a directory — remove it "
                            f"by hand)")
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                done.append(f"deleted {target}")
            except OSError as exc:
                done.append(f"failed {target}: {exc}")
                continue
            else:
                done.append(f"deleted {target}")
            continue
        source = row.source
        if source in seen:
            continue
        seen.add(source)
        if source.is_dir() and not source.is_symlink():
            try:
                shutil.rmtree(source)
            except OSError as exc:
                done.append(f"failed {_short(source, home)}: {exc}")
                continue
        else:
            try:
                source.unlink()
            except FileNotFoundError:
                pass  # already gone — the delete asked for that
            except OSError as exc:
                done.append(f"failed {_short(source, home)}: {exc}")
                continue
        done.append(f"deleted {_short(source, home)}")
        # The destination link still points at the hub path — a dangling
        # symlink is harmless to the harness but litter; drop it.
        if row.dest.is_symlink():
            try:
                if row.dest.resolve() == source.resolve() or \
                        not row.dest.exists():
                    row.dest.unlink()
            except OSError:
                pass
        if row.tier == "shared":
            # Generated wrappers are pruned by the sync below; the shared
            # source disappearing is exactly what pruning watches for.
            continue
    return done


def _delete(rest: list[str], json_view: bool, fail) -> int:
    try:
        rest = expand_value_flags(rest, {"--harness"})
    except ValueError as exc:
        return fail(c("red", str(exc)))
    rest, harness = _harness_arg(rest)
    rest, want_foreign = _flag(rest, "--foreign")
    rest, assume_yes = _flag(rest, "--yes")
    if len(rest) != 1:
        return fail(c("red", "Usage: swe hooks delete <name> "
                             "[--harness=<h>] [--foreign] [--yes]"))
    name = rest[0]
    home = Path.home()
    rows, registry = _rows(home)
    matches = find_script(name, rows)
    if harness:
        harness = _resolve_harness(
            harness, registry, {r.harness for r in rows})
        matches = [r for r in matches if r.harness == harness]
    if not matches:
        return fail(c("red", f"No hook named {name} under "
                             "~/.swe/hooks or its destinations."))
    # A shared hook is one global source across every deploy target —
    # scoping the delete to one harness would still remove the file all
    # of them run. Only per-harness rows may be scoped; a shared-only
    # match refuses rather than quietly deleting for everyone.
    if harness:
        scoped = [r for r in matches if r.tier != "shared"]
        if not scoped:
            return fail(c("red", f"{name} is a shared hook — one source "
                                 "for every harness, so --harness cannot "
                                 "scope a delete. Omit --harness to "
                                 "delete the shared script."))
        matches = scoped
    foreign = [r for r in matches if r.tier == "foreign"]
    if foreign and not want_foreign:
        paths = ", ".join(sorted({_short(r.source, home)
                                  for r in foreign}))
        return fail(c("red",
                      f"{name} is foreign ({paths}) — swe did not put it "
                      f"there. Pass --foreign to delete the real file."))
    if want_foreign:
        matches = foreign
        if not matches:
            return fail(c("red", f"{name} is not a foreign hook — "
                                 "--foreign only applies to foreign rows."))

    # Deleting is confirm-required; --yes answers for a script. A --json
    # caller cannot answer a prompt (stdout must stay one document), so
    # it must pass --yes outright.
    if json_view and not assume_yes:
        return fail(c("red", "delete --json needs --yes — a confirmation "
                             "prompt would corrupt the document."))
    owned = [r for r in matches if r.tier != "foreign"]
    plan_lines = []
    for r in owned:
        plan_lines.append(f"hub source {_short(r.source, home)} "
                          f"({r.tier}, {r.harness})")
    for r in matches:
        if r.tier == "foreign":
            plan_lines.append(f"real file {_short(r.source, home)}")
    if not _confirm(
            f"  Delete {len(matches)} row(s): "
            + "; ".join(plan_lines[:3])
            + ("…" if len(plan_lines) > 3 else "") + "? [y/N] ",
            assume_yes):
        return fail(c("red", "Refused without --yes (or a y at the "
                             "prompt)."))

    done = _delete_row_files(matches, home)
    # Wrappers whose shared source vanished get pruned; if that emptied
    # a _generated dir, its destination link dangles — drop it too.
    _shared.apply_deploy(home, registry)
    for row in matches:
        if row.tier != "shared":
            continue
        link = row.dest.parent
        try:
            if link.is_symlink() and not link.exists():
                link.unlink()
        except OSError:
            pass
    decls = [d for r in matches for d in r.declared]
    failures = any(line.startswith("failed ") for line in done)
    if json_view:
        emit({
            "deleted": [line for line in done
                        if not line.startswith("failed ")],
            "failed": [line for line in done
                       if line.startswith("failed ")],
            "declarations_left": sorted({str(d.settings) for d in decls}),
        })
        return 1 if failures else 0
    for line in done:
        if line.startswith(("failed ", "skipped ")):
            print(f"  {c('yellow', '!')} {sanitize(line)}")
        else:
            print(f"  {c('green', '✓')} {line}")
    if decls:
        files = sorted({sanitize(_short(d.settings, home))
                        for d in decls})
        note = ("declaration stanza(s) stay — remove them yourself: "
                + ", ".join(files))
        print(f"  {c('dim', note)}\n")
    else:
        print()
    return 1 if failures else 0


def _toggle(rest: list[str], json_view: bool, fail, *, off: bool) -> int:
    """``swe hooks disable`` / ``enable``: the .disabled marker + re-plan."""
    try:
        rest = expand_value_flags(rest, {"--harness"})
    except ValueError as exc:
        return fail(c("red", str(exc)))
    rest, harness = _harness_arg(rest)
    verb = "disable" if off else "enable"
    if len(rest) != 1:
        return fail(c("red", f"Usage: swe hooks {verb} <name> "
                             f"[--harness=<h>]"))
    name = rest[0]
    home = Path.home()
    # The re-plan below links and unlinks, so the user's protection list
    # has to be readable first — the rule sync keeps. Unguarded, plan_hooks
    # reloaded it and an unreadable .linkignore escaped as a traceback.
    try:
        patterns = load_linkignore(home)
    except LinkIgnoreError as exc:
        return fail(c("red", f"{exc} — fix or remove "
                             f"~/.swe/.linkignore, then {verb} again."))
    rows, registry = _rows(home)
    matches = find_script(name, rows)
    if harness:
        harness = _resolve_harness(
            harness, registry, {r.harness for r in rows})
        matches = [r for r in matches if r.harness == harness]
    if not matches:
        return fail(c("red", f"No hook named {name} under ~/.swe/hooks."))
    if any(r.tier == "foreign" for r in matches):
        return fail(c("red", f"{name} is a foreign hook — swe cannot "
                             f"{verb} what it does not own."))
    # A shared hook's .disabled marker and its generated wrappers span
    # every harness — scoping to one would still flip them all, so a
    # shared-only match refuses rather than pretending to be local.
    if harness:
        scoped = [r for r in matches if r.tier != "shared"]
        if not scoped:
            return fail(c("red", f"{name} is a shared hook — one marker "
                                 f"and one wrapper set for every harness, "
                                 f"so --harness cannot scope {verb}. "
                                 "Omit --harness."))
        matches = scoped
    # One marker per hub file: shared rows collapse onto their script.
    sources = {r.source for r in matches}
    for source in sources:
        _shared.set_disabled(source.parent, source.name, off)
    # Re-plan and apply so the destination follows the marker right now:
    # disable unlinks, enable links. Shared wrappers (re)generate too.
    if any(r.tier == "shared" for r in matches):
        _shared.apply_deploy(home, registry)
    statuses = plan_hooks(home, patterns=patterns, registry=registry)
    # A shared script's link is its harness's _generated dir — apply that
    # status too so enabling relinks the wrappers it just wrote.
    interesting = set(sources) | {
        _shared.generated_dir(home, r.harness)
        for r in matches if r.tier == "shared"
    }
    results: Counter = Counter()
    for status in statuses:
        if status.source in interesting:
            results[apply_hook_status(status, home)] += 1
    word = "disabled" if off else "enabled"
    if json_view:
        return emit({
            "name": name,
            "state": word,
            "sources": [str(s) for s in sorted(sources)],
            "links": dict(results),
        })
    print(f"  {c('green', '✓')} {word} {name}: "
          + ", ".join(sorted({_short(s, home) for s in sources})))
    if off:
        note = ("declarations stay — the harness sees a missing script, "
                "which its contract tolerates (or a default JSON answer "
                "for generated wrappers)")
    else:
        note = ("linked back in (swe hooks shows any link it could not "
                "place); the harness runs it again wherever its settings "
                "declare it")
    print(f"  {c('dim', note)}\n")
    return 0


def _sync(rest: list[str], json_view: bool, fail) -> int:
    """``swe hooks sync``: regenerate wrappers, then apply the link plan."""
    rest, force = _flag(rest, "--force")
    if rest:
        return fail(c("red", f"Unexpected args: {' '.join(rest)}"))
    home = Path.home()
    registry = load_registry(home)
    # The ignore file is the user's protection list — syncing without it
    # could replace paths it was written to protect, so an unreadable one
    # stops the run before any generated write or link change, the same
    # way swe init refuses.
    try:
        patterns = load_linkignore(home)
    except LinkIgnoreError as exc:
        return fail(c("red", f"{exc} — fix or remove "
                             "~/.swe/.linkignore, then sync again."))
    from swe.init.migrate import move_renamed_hook_dirs

    # Same one-shot rename swe init runs, ahead of the deploy and plan.
    moved = move_renamed_hook_dirs(home, registry=registry, patterns=patterns)
    written, pruned = _shared.apply_deploy(home, registry)
    statuses = plan_hooks(home, patterns=patterns, registry=registry)
    results: Counter = Counter()
    for status in statuses:
        results[apply_hook_status(status, home, force)] += 1
    if json_view:
        return emit({
            "generated": [str(p) for p in written],
            "pruned": [str(p) for p in pruned],
            "links": dict(results),
        })
    for note in moved:
        print(f"  {c('cyan', '~')} {note}")
    if written:
        for path in written:
            print(f"  {c('cyan', '+')} wrote {_short(path, home)}")
    if pruned:
        for path in pruned:
            print(f"  {c('dim', '-')} pruned {_short(path, home)}")
    changed = {k: v for k, v in results.items()
               if k not in ("linked", "skipped", "ignored")}
    if changed:
        print("  " + "  ".join(f"{k}: {v}" for k, v in sorted(
            changed.items())))
    linked = results.get("linked", 0)
    total = sum(results.values())
    print(f"  {c('dim', f'{linked}/{total} hook links in place')}\n")
    return 0
