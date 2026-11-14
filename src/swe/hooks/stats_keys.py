"""Which stats names a hook row's run counts come from.

The one implementation behind ``stats_keys`` in ``swe hooks list --json``
and in the web Hooks page's rows: ``swe hooks stats`` names a hook by the
script its runs carried, which is not always the name the list shows.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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


def stats_keys(rows, home: Path) -> list[list[str]]:
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
