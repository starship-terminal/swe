"""Which script a hook run was, and which ``swe hooks list`` row it is.

A hook run has two names that can disagree: the hook log names it by the
stem of the script the shim ran (``notify``), and a Claude Code transcript
by the command line it ran (``.claude/notify`` for the generated wrapper
``~/.claude/hooks/_generated/notify.py``). Matching those names is a
guess, so a run carries the script it ran instead whenever that is
knowable:

* the shim records the shared script it runs, resolved (``script`` in the
  log line); ``shim_runtime.record`` called by a hook itself records the
  running program unless told otherwise;
* a transcript run's command is read the way ``swe hooks list`` reads a
  declared command (``inventory._executed_scripts``: quotes, ``~`` and
  ``$HOME``, interpreters, ``sh -c``), with symlinks resolved and a
  generated wrapper resolved to the shared script it wraps. A command
  whose path holds a variable swe cannot know (``${CLAUDE_PLUGIN_ROOT}``)
  or is relative has no script.

``Owners`` joins runs to inventory rows. A run with a script belongs to
the row whose source is that file, or whose folder holds it as a declared
executable (a directory hook's undeclared helper files own nothing). A
run with no script falls back to its name: a log line to the stems a row
is logged under (its file's, or its declared executables'), a transcript
run to ``command_hook`` of the row's declared commands that have no
resolvable path. A name or path more than one source answers to belongs
to none of them: those runs make the rows ``uncertain`` instead, and the
pages show dashes there rather than another hook's numbers.

Nothing here prints or writes.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

# How many lines of a generated wrapper to read for its shared source.
_WRAPPER_HEAD = 8
_SHARED_LINE = "# Shared source: "
_SHARED_ASSIGN = re.compile(r"^_SHARED = (.+)$")

# ``f="$HOME/x/y.py"; ... python3 "$f"``: a shell variable set once to a
# path and then run. Its value is spliced into the references so the
# command reads like a plain path.
_ASSIGNMENT = re.compile(
    r"(?:^|(?<=[\s;&|(]))([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:\"([^\"`\\]*)\"|'([^']*)'|([^\s;&|()<>\"'`]+))")


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _inside(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip(os.sep) + os.sep)


def wrapped_source(path: str) -> str | None:
    """The shared script a generated wrapper at ``path`` runs, resolved;
    None for any other file."""
    from swe.hooks import shared as _shared

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = [handle.readline() for _ in range(_WRAPPER_HEAD)]
    except (OSError, ValueError):
        return None
    if not any(mark in line for line in head
               for mark in _shared._GENERATED_MARKS):
        return None
    for line in head:
        line = line.rstrip("\n")
        if line.startswith(_SHARED_LINE):
            value = line[len(_SHARED_LINE):].strip()
            if os.path.isabs(value):
                return _real(value)
        match = _SHARED_ASSIGN.match(line)
        if match:
            try:
                value = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str) and os.path.isabs(value):
                return _real(value)
    return None


def _splice(command: str) -> str:
    """``command`` with each variable assigned exactly once to a path
    replaced by that path where it is used."""
    values: dict[str, list[str]] = {}
    for match in _ASSIGNMENT.finditer(command):
        value = next(g for g in match.groups()[1:] if g is not None)
        values.setdefault(match.group(1), []).append(value)
    out = command
    for name, found in values.items():
        value = found[0]
        if len(found) != 1 or "/" not in value or f"${name}" in value \
                or "${" + name + "}" in value:
            continue
        refs = re.compile(r"\$\{" + name + r"\}|\$" + name
                          + r"(?![A-Za-z0-9_])")
        out = refs.sub(lambda _m: value, out)
    return out


class Resolver:
    """The script a command runs, resolved, cached for one read.

    One per ``collect``: the answer depends on the files on disk, so it
    is never kept across reads.
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self._cache: dict[str, tuple[str | None, bool]] = {}

    def script(self, command: str) -> tuple[str | None, bool]:
        """(the resolved script ``command`` runs or None, whether it was
        reached through a generated wrapper)."""
        command = command or ""
        hit = self._cache.get(command)
        if hit is None:
            hit = self._cache[command] = self._resolve(command)
        return hit

    def _resolve(self, command: str) -> tuple[str | None, bool]:
        from swe.hooks.inventory import _executed_scripts

        try:
            # A run's script may be gone since (a pruned wrapper): its
            # path still names it.
            found = _executed_scripts(_splice(command), self.home,
                                      must_exist=False)
        except Exception:  # noqa: BLE001 - an odd command has no script
            return None, False
        resolved = set()
        wrapped = False
        for path in found:
            real = _real(str(path))
            shared = wrapped_source(real)
            if shared:
                real, wrapped = shared, True
            resolved.add(real)
        if len(resolved) != 1:
            # Nothing, or two scripts in one command: no one identity.
            return None, False
        return resolved.pop(), wrapped

    def name(self, command: str) -> tuple[str, str | None]:
        """(hook name, script) for a transcript's command: a wrapper's run
        takes the shared script's stem, the name the shim logs it under;
        anything else is ``transcripts.command_hook``."""
        from swe.hooks.events import hook_stem
        from swe.hooks.transcripts import command_hook

        script, wrapped = self.script(command)
        return (hook_stem(script) if wrapped else command_hook(command)), \
            script


def inventory_rows(home: Path) -> list:
    """Every ``swe hooks list`` row, built the way the list builds them;
    none when the inventory cannot be read (the counts then belong to no
    row, which is still right)."""
    try:
        from swe.hooks.inventory import all_rows, declarations_by_harness
        from swe.init.hooks import plan_hooks
        from swe.init.layout import (
            LinkIgnoreError,
            load_linkignore,
            load_registry,
        )

        registry = load_registry(home)
        try:
            patterns = load_linkignore(home)
        except LinkIgnoreError:
            patterns = []
        statuses = plan_hooks(home, patterns=patterns, registry=registry)
        declared = declarations_by_harness(home, registry)
        return all_rows(home, registry, statuses, declared)
    except Exception:  # noqa: BLE001 - counts without owners beat none
        return []


class Owners:
    """Which inventory row each run belongs to (see the module docstring).

    An owner is a row's source resolved; ``sources_of`` gives the
    ``source_path`` spellings the list prints for it (the rows of one
    shared script all share one).
    """

    def __init__(self, rows, resolver: Resolver):
        from swe.hooks.events import hook_stem
        from swe.hooks.transcripts import command_hook

        self._spellings: dict[str, set[str]] = {}
        self._folders: set[str] = set()
        # key -> {owner: the harnesses of the rows that claim it}
        paths: dict[str, dict[str, set[str]]] = {}
        helper: dict[str, dict[str, set[str]]] = {}
        transcript: dict[str, dict[str, set[str]]] = {}

        def claim(table: dict, key: str | None, owner: str) -> None:
            if key:
                table.setdefault(key, {}).setdefault(owner, set()).add(
                    str(getattr(row, "harness", "")))

        for row in rows:
            source = str(row.source)
            owner = _real(source)
            self._spellings.setdefault(owner, set()).add(source)
            is_dir = os.path.isdir(owner)
            if is_dir:
                self._folders.add(owner)
            else:
                claim(paths, owner, owner)
                dest = str(row.dest)
                # The file the harness runs, when it stands for this
                # source: a link to it, a shared script's wrapper, or a
                # path with nothing there now (a run from before it went).
                # A real file of its own in the way is another script.
                if getattr(row, "tier", "") == "shared" \
                        or not os.path.lexists(dest) or _real(dest) == owner:
                    claim(paths, _real(dest), owner)
                claim(helper, hook_stem(row.source.name), owner)
            for decl in getattr(row, "declared", []):
                script, _wrapped = resolver.script(decl.command)
                if script is None:
                    claim(transcript, command_hook(decl.command), owner)
                elif is_dir and _inside(script, owner):
                    # Resolved, so a path through the harness's link to
                    # the folder lands inside it too.
                    claim(paths, script, owner)
                    claim(helper, hook_stem(os.path.basename(script)), owner)
        self._paths = paths
        self._helper = helper
        self._transcript = transcript

    def canonical(self, script: str | None) -> str | None:
        """``script`` as the one file it stands for: the source of the file
        row that alone claims it (a shared script for its wrapper, a hub
        file for its harness link), else itself. A directory's executables
        stay apart: two of them are two scripts even under one row."""
        if not script:
            return script
        owners = self._paths.get(script, {})
        if len(owners) == 1:
            owner = next(iter(owners))
            if owner not in self._folders:
                return owner
        return script

    @classmethod
    def load(cls, home: Path, resolver: Resolver | None = None) -> "Owners":
        return cls(inventory_rows(home), resolver or Resolver(home))

    def sources_of(self, owner: str | None) -> list[str]:
        """The ``source_path`` values the list prints for ``owner``."""
        return sorted(self._spellings.get(owner, ())) if owner else []

    def name_of(self, owner: str) -> str:
        """The list's name for ``owner`` without its script extension:
        ``bundle``, ``prose-guard``."""
        from swe.hooks.events import strip_script_extension

        spelling = (self.sources_of(owner) or [owner])[0]
        return strip_script_extension(os.path.basename(spelling))

    def _claims(self, event: dict) -> set[str]:
        script = event.get("script")
        if script:
            return set(self._paths.get(script, {}))
        table = self._helper if event.get("source") == "helper" \
            else self._transcript
        found = table.get(event.get("hook") or "", {})
        # A name is narrowed to the rows on the harness that ran it first:
        # the codex copy of a per-harness script cannot have run on Claude.
        # A shared script deployed to a harness has a row there; only when
        # no row on that harness answers (one archived since) do the rest.
        here = {owner for owner, harnesses in found.items()
                if event.get("harness") in harnesses}
        return here or set(found)

    def assign(self, events: list[dict]) -> None:
        """Set on every event: ``_owner`` (an owner, or None),
        ``_owner_name`` (its list name, ``name_of``), ``_sources`` (its
        ``source_path`` spellings) and ``_maybe`` (the spellings of every
        owner a run that has none could be)."""
        names: dict[str, str] = {}
        for event in events:
            claims = self._claims(event)
            owner = next(iter(claims)) if len(claims) == 1 else None
            event["_owner"] = owner
            if owner is not None and owner not in names:
                names[owner] = self.name_of(owner)
            event["_owner_name"] = names.get(owner) if owner else None
            event["_sources"] = self.sources_of(owner)
            event["_maybe"] = () if owner else tuple(sorted(
                spelling for claim in claims
                for spelling in self.sources_of(claim)))
