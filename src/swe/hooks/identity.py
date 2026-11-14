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
import json
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


_PLUGIN_ROOT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT(?![A-Za-z0-9_])")
_PROJECT_DIR = re.compile(r"\$\{CLAUDE_PROJECT_DIR\}|\$CLAUDE_PROJECT_DIR(?![A-Za-z0-9_])")


def _plugin_commands(value, out: list[str]) -> None:
    """Every ``command`` string in a plugin's hooks definition."""
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and value.get("type", "command") == "command":
            out.append(command)
        for item in value.values():
            _plugin_commands(item, out)
    elif isinstance(value, list):
        for item in value:
            _plugin_commands(item, out)


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def plugin_roots(home: Path) -> dict[str, set[str]]:
    """``{declared command: {install folder}}`` for every Claude Code plugin
    installed under ``home``: the hooks each plugin's ``hooks/hooks.json``
    (or its manifest's ``hooks``) declares, read from the install record
    ``~/.claude/plugins/installed_plugins.json``. A transcript stores a
    plugin hook's command with ``${CLAUDE_PLUGIN_ROOT}`` unexpanded and
    nothing else about the plugin; the exact command string is what ties
    a run to the plugin that declares it."""
    record = _read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    installs = record.get("plugins") if isinstance(record, dict) else None
    out: dict[str, set[str]] = {}
    if not isinstance(installs, dict):
        return out
    for entries in installs.values():
        for entry in entries if isinstance(entries, list) else []:
            folder = entry.get("installPath") if isinstance(entry, dict) else None
            if not isinstance(folder, str) or not os.path.isdir(folder):
                continue
            found: list[str] = []
            _plugin_commands(_read_json(Path(folder) / "hooks" / "hooks.json"),
                             found)
            manifest = _read_json(Path(folder) / ".claude-plugin" / "plugin.json")
            hooks = manifest.get("hooks") if isinstance(manifest, dict) else None
            if isinstance(hooks, str):
                hooks = [hooks]
            if isinstance(hooks, list) and all(isinstance(h, str) for h in hooks):
                for rel in hooks:
                    _plugin_commands(_read_json(Path(folder) / rel), found)
            elif isinstance(hooks, dict):
                _plugin_commands(hooks, found)
            for command in found:
                out.setdefault(command, set()).add(_real(folder))
    return out


class Resolver:
    """The script a command runs, resolved, cached for one read.

    One per ``collect``: the answer depends on the files on disk, so it
    is never kept across reads. Besides what ``swe hooks list`` resolves,
    it expands the two variables Claude Code sets for a hook where they
    are knowable: ``$CLAUDE_PLUGIN_ROOT`` to the one installed plugin that
    declares that exact command (``plugin_roots``), and
    ``$CLAUDE_PROJECT_DIR`` to the session's project folder (the first
    ``cwd`` its transcript records) when one is given.
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self._cache: dict[tuple[str, str | None],
                          tuple[tuple[str, bool], ...]] = {}
        self._names_project: dict[str, bool] = {}
        self._named: dict[tuple[str, str | None], tuple[str, str | None]] = {}
        self._plugins: dict[str, set[str]] | None = None
        self._generated: set[str] | None = None

    def scripts(self, command: str, project: str | None = None) -> list[str]:
        """Every resolved script ``command`` runs, in order, one per file
        (``a.py && b.py`` runs two)."""
        return list(dict.fromkeys(
            path for path, _ in self._found(command, project)))

    def script(self, command: str,
               project: str | None = None) -> tuple[str | None, bool]:
        """(the one resolved script ``command`` runs, or None when it runs
        none or several; whether it was reached through a generated
        wrapper). A run of a command with two scripts is neither's: it is
        one run, and swe cannot say which file made its decision."""
        found = self._found(command, project)
        if len({path for path, _ in found}) != 1:
            return None, False
        return found[0]

    def _found(self, command: str,
               project: str | None) -> tuple[tuple[str, bool], ...]:
        command = command or ""
        # The project only matters to a command that names it.
        names = self._names_project.get(command)
        if names is None:
            names = self._names_project[command] = bool(
                _PROJECT_DIR.search(command))
        key = (command, project if names else None)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._resolve(command, key[1])
        return hit

    def _variables(self, command: str, project: str | None) -> dict[str, str]:
        """The hook variables knowable for ``command``: given to the
        resolver as values for its parsed tokens, never spliced into the
        command text (a folder named ``My "Demo" App`` would be re-read
        as shell)."""
        out: dict[str, str] = {}
        if _PLUGIN_ROOT.search(command):
            if self._plugins is None:
                self._plugins = plugin_roots(self.home)
            roots = self._plugins.get(command, set())
            if len(roots) == 1:
                out["CLAUDE_PLUGIN_ROOT"] = next(iter(roots))
        if project and _PROJECT_DIR.search(command):
            out["CLAUDE_PROJECT_DIR"] = project
        return out

    def _resolve(self, command: str,
                 project: str | None) -> tuple[tuple[str, bool], ...]:
        from swe.hooks.inventory import _executed_scripts

        try:
            # A run's script may be gone since (a pruned wrapper): its
            # path still names it.
            found = _executed_scripts(
                _splice(command), self.home, must_exist=False,
                variables=self._variables(command, project))
        except Exception:  # noqa: BLE001 - an odd command has no script
            return ()
        out = []
        for path in found:
            real = _real(str(path))
            shared = wrapped_source(real) or self._vanished_wrapper(str(path))
            out.append((shared, True) if shared else (real, False))
        return tuple(out)

    def _vanished_wrapper(self, path: str) -> str | None:
        """The shared script a generated wrapper path stood for, when the
        wrapper (or its whole ``_generated`` folder) is gone: nothing is
        at ``path``, it has the shape swe writes (``<a harness's hooks
        root>/_generated/<name>``), and ``~/.swe/hooks/shared/<name>``
        exists. Anything that is there is never taken for a wrapper."""
        from swe.hooks import shared as _shared

        if os.path.lexists(path):
            return None
        folder, name = os.path.split(path)
        if os.path.basename(folder) != _shared.GENERATED_DIRNAME:
            return None
        if self._generated is None:
            self._generated = self._generated_folders()
        if folder not in self._generated and _real(folder) not in self._generated:
            return None
        source = _shared.shared_dir(self.home) / name
        return _real(str(source)) if source.is_file() else None

    def _generated_folders(self) -> set[str]:
        """``<root>/_generated`` for every harness's hooks root, as given
        and resolved."""
        try:
            from swe.hooks import shared as _shared
            from swe.hooks.inventory import _harness_names
            from swe.init.hooks import hook_root
            from swe.init.layout import load_registry
            from swe import paths as _p

            registry = load_registry(self.home)
            out: set[str] = set()
            for harness in _harness_names(_p.hooks_dir_for(self.home), registry):
                root, _why = hook_root(registry, harness, self.home)
                if root is not None:
                    folder = str(root / _shared.GENERATED_DIRNAME)
                    out |= {folder, _real(folder)}
            return out
        except Exception:  # noqa: BLE001 - no roots: no vanished wrappers
            return set()

    def name(self, command: str,
             project: str | None = None) -> tuple[str, str | None]:
        """(hook name, script) for a transcript's command: a wrapper's run
        takes the shared script's stem, the name the shim logs it under;
        anything else is ``transcripts.command_hook``."""
        from swe.hooks.events import hook_stem
        from swe.hooks.transcripts import command_hook

        hit = self._named.get((command, project))
        if hit is None:
            script, wrapped = self.script(command, project)
            hit = self._named[(command, project)] = (
                hook_stem(script) if wrapped else command_hook(command), script)
        return hit


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
                # The file itself; a harness link to it resolves here too.
                claim(paths, owner, owner)
                wrapper = self._wrapper_path(row, owner, resolver.home)
                if wrapper:
                    claim(paths, wrapper, owner)
                claim(helper, hook_stem(row.source.name), owner)
            for decl in getattr(row, "declared", []):
                script, _wrapped = resolver.script(decl.command)
                if script is None:
                    # No one script (a variable, or several files): the
                    # transcript names the run by its command.
                    name = command_hook(decl.command)
                    claim(transcript, name, owner)
                    if is_dir and not resolver.scripts(decl.command):
                        # Nothing resolvable at all, and the inventory
                        # joined it to this folder by name: a log line of
                        # that executable carries its bare stem.
                        claim(helper, name.rsplit("/", 1)[-1], owner)
                if is_dir:
                    # Every file the declaration runs inside the folder is
                    # one of its executables, even two in one command: a
                    # run each logs for itself is the folder's. Resolved,
                    # so a path through the harness's link lands inside.
                    for path in resolver.scripts(decl.command):
                        if _inside(path, owner):
                            claim(paths, path, owner)
                            claim(helper, hook_stem(os.path.basename(path)),
                                  owner)
        self._paths = paths
        self._helper = helper
        self._transcript = transcript

    @staticmethod
    def _wrapper_path(row, owner: str, home: Path) -> str | None:
        """The resolved path of a shared row's wrapper, when runs through it
        are this script's: the file there is a generated wrapper for this
        source, or nothing is there now and the path is where swe writes
        its wrapper (a run from before it was pruned). Anything else at
        that path (a link conflict, another installer's ``_generated``
        folder) is another script, and keeps its own runs."""
        if getattr(row, "tier", "") != "shared":
            return None
        dest = _real(str(row.dest))
        if dest == owner:
            return None
        if os.path.lexists(dest):
            return dest if wrapped_source(dest) == owner else None
        from swe.hooks import shared as _shared

        expected = _real(str(_shared.generated_dir(home, row.harness)
                             / row.source.name))
        return dest if dest == expected else None

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
        seen: dict[tuple, set[str]] = {}
        spellings: dict[str, list[str]] = {}
        for event in events:
            # Many runs share one answer: memoize on what decides it.
            memo = (event.get("script"), event.get("source") == "helper",
                    event.get("hook"), event.get("harness"))
            claims = seen.get(memo)
            if claims is None:
                claims = seen[memo] = self._claims(event)
            owner = next(iter(claims)) if len(claims) == 1 else None
            event["_owner"] = owner
            if owner is not None and owner not in names:
                names[owner] = self.name_of(owner)
            event["_owner_name"] = names.get(owner) if owner else None
            if owner is not None and owner not in spellings:
                spellings[owner] = self.sources_of(owner)
            event["_sources"] = spellings[owner] if owner else []
            event["_maybe"] = () if owner or not claims else tuple(sorted(
                spelling for claim in claims
                for spelling in self.sources_of(claim)))
