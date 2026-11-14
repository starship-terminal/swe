"""Where each harness declares its hooks, and what it declares.

A script under ``~/.swe/hooks/<harness>/`` does nothing until the
harness's own settings file names it: ``~/.claude/settings.json`` carries a
``hooks`` map, ``~/.codex/hooks.json`` is that same map at its top level,
``~/.cursor/hooks.json`` spells the same idea flat. Two shapes cover them,
keyed by the contract's ``schema`` in ``contracts.py``:

  grouped — ``{Event: [{matcher?, hooks: [{type, command, timeout?}]}]}``
  flat    — ``{Event: [{command, timeout?, matcher?}]}`` (cursor)

Muse indirections once more: its settings file names no hooks at all, it
points at a managed file through ``managed_hooks_path`` — the contract's
``managed_hooks_key`` says which field to follow.

Discovery mirrors ``hook_root``'s rule: ``capabilities.hooks.settings`` in
harness.json wins first (one path or a list, each ``~/``-relative or
absolute); without it, ``SETTINGS_FALLBACK`` holds the files the contract
registry records, and the basenames are probed beside the hooks root for
a harness that declared only ``capabilities.hooks.root``.

Everything here tolerates: a missing file, unparseable JSON, and schema
drift (``hooks`` that is not a dict, groups that are not lists, entries
that are not objects) each read as no declarations, never an error.

The one write is ``drop_script_declarations``: Doctor's "Drop the
declaration" runs it, and only after ``declaration_managed_elsewhere``
clears the file (a symlinked or unwritable file is owned by whatever
manages it) and a backup lands in ~/.swe/backups. Everything else is
read-only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from swe.init.hooks import hook_root
from swe.hooks.contracts import CONTRACTS, contract_for
from swe.hooks.models import Declaration

# The files known harnesses declare hooks in, home-relative, keyed by
# registry name like every other fallback table — the contract registry's
# ``settings`` rows.
SETTINGS_FALLBACK: dict[str, tuple[Path, ...]] = {
    name: contract.settings
    for name, contract in CONTRACTS.items()
    if contract.settings
}

# Probed beside the hooks root for harnesses the table above does not name:
# <hooks root's parent>/settings.json, then hooks.json, settings.local.json.
_PROBE_BASENAMES = ("settings.json", "hooks.json", "settings.local.json")

# Extensions a command token ends with to count as naming a script.
_SCRIPT_EXTS = (
    ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".ps1", ".cmd",
)

# ${HOME}, ${HOME-}, ${HOME-...}: the spellings hook commands actually use.
_HOME_VAR_RE = re.compile(r"\$\{HOME[^}]*\}|\$HOME\b")


def _expand_declared(value: str, home: Path) -> Path:
    """A ``capabilities.hooks.settings`` path: ``~/``-relative or absolute."""
    raw = str(value)
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def _declared_settings(caps: dict, home: Path) -> list[Path]:
    value = caps.get("settings")
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [_expand_declared(v, home) for v in items if isinstance(v, str)]


def configured_settings(registry: dict, name: str, home: Path) -> list[Path]:
    """``capabilities.hooks.settings`` paths, even before they exist.

    ``swe hooks new`` names a file to create; filtering to existing files
    the way ``settings_files`` does would silently swap a configured path
    for a guess.
    """
    entry = registry.get(name)
    caps = ((entry if isinstance(entry, dict) else {})
            .get("capabilities") or {}).get("hooks")
    if isinstance(caps, dict):
        return _declared_settings(caps, home)
    return []


def settings_files(registry: dict, name: str, home: Path) -> list[Path]:
    """Every settings file that may declare ``name``'s hooks, existing only.

    The capability wins outright when present — it replaces the fallback,
    it does not add to it, the same way ``capabilities.hooks.root`` does.
    ``supported: false`` turns discovery off even for a table harness.
    """
    entry = registry.get(name)
    caps = ((entry if isinstance(entry, dict) else {})
            .get("capabilities") or {}).get("hooks")
    out: list[Path] = []
    if isinstance(caps, dict):
        if "supported" in caps and not caps["supported"]:
            return []
        declared = _declared_settings(caps, home)
        if declared:
            out.extend(declared)
    if not out:
        for rel in SETTINGS_FALLBACK.get(name, ()):
            out.append(home / rel)
        root, _why = hook_root(registry, name, home)
        if root is not None and root.parent != home:
            for base in _PROBE_BASENAMES:
                out.append(root.parent / base)
    # De-dupe, keep only real files: a missing file is not a declaration.
    seen: set[Path] = set()
    files: list[Path] = []
    for path in out:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        files.append(path)
    return files


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _grouped_declarations(
    data: dict, path: Path, harness: str
) -> list[Declaration]:
    """``{Event: [{matcher?, hooks: [{type, command, timeout?}]}]}``."""
    hooks_map = data.get("hooks")
    if not isinstance(hooks_map, dict):
        return []
    out: list[Declaration] = []
    for event, groups in hooks_map.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            matcher = matcher if isinstance(matcher, str) else ""
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                timeout = hook.get("timeout")
                out.append(Declaration(
                    harness=harness,
                    event=str(event),
                    matcher=matcher,
                    timeout=timeout if isinstance(timeout, (int, float)) else None,
                    command=command,
                    settings=path,
                ))
    return out


def _flat_declarations(
    data: dict, path: Path, harness: str
) -> list[Declaration]:
    """Cursor's ``{version: 1, hooks: {event: [{command, ...}]}}``.

    The list items are the hooks themselves — no matcher-group wrapper —
    with ``matcher`` and ``timeout`` on the entry directly.
    """
    hooks_map = data.get("hooks")
    if not isinstance(hooks_map, dict):
        return []
    out: list[Declaration] = []
    for event, entries in hooks_map.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            matcher = entry.get("matcher")
            timeout = entry.get("timeout")
            out.append(Declaration(
                harness=harness,
                event=str(event),
                matcher=matcher if isinstance(matcher, str) else "",
                timeout=timeout if isinstance(timeout, (int, float)) else None,
                command=command,
                settings=path,
            ))
    return out


def declarations(
    path: Path,
    harness: str,
    registry: dict | None = None,
    home: Path | None = None,
) -> list[Declaration]:
    """The hook commands one settings file declares, shape-tolerant.

    The harness's contract picks the parser. Anything that does not match
    the schema's shape is skipped entry by entry rather than failing the
    file: a harness writing a different schema under ``hooks`` is drift
    to tolerate, not an error.
    """
    home = home or Path.home()
    data = _read_json(path)
    if data is None:
        return []
    contract = contract_for(registry or {}, harness, home)
    if contract.managed_hooks_key:
        # muse: the settings file only points at the managed hooks file.
        target = data.get(contract.managed_hooks_key)
        if not isinstance(target, str) or not target.strip():
            return []
        managed = _expand_declared(target.strip(), home)
        managed_data = _read_json(managed)
        if managed_data is None:
            return []
        data, path = managed_data, managed
    if contract.schema == "flat":
        return _flat_declarations(data, path, harness)
    return _grouped_declarations(data, path, harness)


def harness_declarations(
    registry: dict, name: str, home: Path
) -> list[Declaration]:
    """Every declaration across all of ``name``'s settings files."""
    out: list[Declaration] = []
    for path in settings_files(registry, name, home):
        out.extend(declarations(path, name, registry, home))
    return out


def declaration_managed_elsewhere(path: Path) -> bool:
    """Whether ``check`` should call this file managed, not editable.

    A declaration file that is a symlink (nix store, dotfiles repo) or
    simply not writable is owned by whatever manages it — reporting
    "edit this file" sends the user at a file they cannot change, so the
    check note says "declaration managed elsewhere" instead.
    """
    if path.is_symlink():
        return True
    try:
        return path.is_file() and not os.access(path, os.W_OK)
    except OSError:
        return False


def _unchanged(path: Path, before) -> None:
    """Refuse a write when the file moved under the read.

    The drop helpers read, edit and write a whole settings file; a plugin
    toggle or a hand edit landing between the read and the write would be
    silently reverted. ``before`` is the ``os.stat`` taken just after the
    read.
    """
    if before is None:
        return
    try:
        now = path.stat()
    except OSError:
        return
    if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
        raise ValueError(f"{path} changed while it was being read; run it again")


def drop_script_declarations(
    path: Path,
    targets,
    registry: dict,
    harness: str,
    home: Path,
) -> int:
    """Remove the hook entries whose commands invoke a ``targets`` token.

    Doctor's deterministic half of a declared-missing fix. ``targets``
    are the finding's missing tokens (the resolved path when one could
    be computed, else the token as written): one basename can be missing
    in one command and alive in another, so matching is by token, never
    by basename. Raises rather than reports: the caller shows the
    exception's text, as every doctor runner does. Refuses a file
    managed elsewhere (symlinked, unwritable), a harness whose settings
    file only points at a generated one, and a file that changed under
    the read. Backs the file up before writing. Returns how many entries
    came out.
    """
    from swe.paths import atomic_write_text, backup_tree

    keys = {str(t) for t in targets}
    if not keys:
        raise ValueError("nothing to drop: no missing tokens named")
    contract = contract_for(registry, harness, home)
    if contract.managed_hooks_key:
        raise ValueError(
            f"{path} only points at a generated file; edit what generates it")
    if declaration_managed_elsewhere(path):
        raise ValueError(f"{path} is managed elsewhere; edit it there")
    try:
        before = path.stat()
    except OSError:
        before = None
    data = _read_json(path)
    if data is None:
        raise ValueError(f"{path} is not a JSON object")
    hooks_map = data.get("hooks")
    if not isinstance(hooks_map, dict):
        raise ValueError(f"{path} declares no hooks")

    def invokes_target(command) -> bool:
        if not isinstance(command, str):
            return False
        for raw in split_command(command):
            token = raw.strip("'\"").rstrip(";")
            if not is_script_token(token):
                continue
            expanded = expand_command_path(token, home)
            if (str(expanded) if expanded is not None else token) in keys:
                return True
        return False

    removed = 0
    if contract.schema == "flat":
        for event, entries in list(hooks_map.items()):
            if not isinstance(entries, list):
                continue
            kept = [e for e in entries if not (
                    isinstance(e, dict) and invokes_target(e.get("command")))]
            removed += len(entries) - len(kept)
            if kept:
                hooks_map[event] = kept
            else:
                del hooks_map[event]
    else:
        for event, groups in list(hooks_map.items()):
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                hooks = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(hooks, list):
                    kept_groups.append(group)
                    continue
                kept = [h for h in hooks if not (
                        isinstance(h, dict) and invokes_target(h.get("command")))]
                removed += len(hooks) - len(kept)
                if kept:
                    group["hooks"] = kept
                    kept_groups.append(group)
            if kept_groups:
                hooks_map[event] = kept_groups
            else:
                del hooks_map[event]
    if not removed:
        raise ValueError(f"{path} declares no hook naming a missing script")
    _unchanged(path, before)
    backup_tree(path, home)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return removed


def split_command(command: str) -> list[str]:
    """One hook command's tokens, quote-aware when the quoting balances.

    ``bash "/path/my guard.sh"`` is one path, not two tokens; a command
    whose quotes do not balance (shell fragments are common in the wild)
    falls back to whitespace splitting rather than failing.
    """
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def basenames_in(command: str) -> set[str]:
    """Every token basename a hook command could be invoking.

    Quoting and trailing shell punctuation are stripped first: the real
    files carry commands like ``python3 "/home/x/.claude/hooks/guard.py"``
    and ``bash '/x/y.sh' session``.
    """
    out: set[str] = set()
    for raw in split_command(command):
        token = raw.strip("'\"").rstrip(";")
        if not token or token.startswith("-"):
            continue
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base:
            out.add(base)
    return out


def is_script_token(token: str) -> bool:
    """Whether a command token names a script file (by its extension)."""
    base = token.strip("'\"").rstrip(";").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base.endswith(_SCRIPT_EXTS)


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand_command_path(token: str, home: Path,
                        variables: dict[str, str] | None = None) -> Path | None:
    """Resolve a command token to a filesystem path, or None if unverifiable.

    ``~`` and the ``$HOME`` spellings expand against ``home``; any other
    variable (``$SUPERSET_HOME_DIR`` is the real one in the wild) stays
    literal after ``expandvars`` and marks the token unverifiable — a path
    swe cannot resolve is "unknown", never "missing".

    ``variables`` are values the caller knows (a plugin's install folder
    for ``$CLAUDE_PLUGIN_ROOT``). They go into the token as it was already
    parsed, after every other expansion, so a value's quotes, spaces, ``$``
    and backslashes are kept as they are, never read as shell or as
    another variable.
    """
    text = token.strip("'\"").rstrip(";")
    marks: dict[str, str] = {}
    if variables:
        def hold(match):
            name = match.group(1) or match.group(2)
            if name not in variables:
                return match.group(0)
            mark = f"\x00{len(marks)}\x00"
            marks[mark] = variables[name]
            return mark

        text = _VAR_RE.sub(hold, text)
    text = _HOME_VAR_RE.sub(str(home), text)
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    elif text.startswith("~"):
        # ``~user/x``: another account's home, which ``home`` says nothing
        # about. Spliced in, ``~root/x.py`` read as ``<home>root/x.py``,
        # reported missing, and doctor offered to drop a valid hook.
        return None
    text = os.path.expandvars(text)
    if "$" in text:
        return None
    for mark, value in marks.items():
        text = text.replace(mark, value)
    # A relative token (hooks/x.sh, ./x.sh) resolves against a cwd swe does
    # not know: unverifiable.
    if not Path(text).is_absolute():
        return None
    return Path(text)
