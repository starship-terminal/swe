"""The join ``swe hooks`` renders: hub scripts x link state x declarations.

``plan_hooks`` says where each ``~/.swe/hooks/<harness>/<script>`` should
land and what state the link is in; ``settings.py`` says which of those
scripts the harness will actually run. One row per script x destination.

The drift checks read the same join the other way: declarations that point
at nothing on disk, hub scripts linked but never declared, hub scripts the
destination never received. Hooks owned by other installers (herdr writes
into ``~/.claude/hooks``) are not drift — they live at the destination
without a hub copy, which is exactly the shape ``init/hooks.py``'s
docstring describes — so a finding needs the script to be missing from
*both* the hub and the destination, or unverifiable paths to stay silent.

Nothing in this module prints.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from swe import paths as _paths
from swe.init.hooks import (
    DISABLED_DETAIL,
    HOOK_FALLBACK,
    RESERVED_HUB_DIRS,
    UNKNOWN_HARNESS_DETAIL,
    hook_root,
    plan_hooks,
)
from swe.init.layout import LinkStatus, load_registry
from swe.hooks.contracts import contract_for
from swe.hooks.models import Declaration, HookDrift, HookRow
from swe.hooks.settings import (
    SETTINGS_FALLBACK,
    declaration_managed_elsewhere,
    expand_command_path,
    harness_declarations,
    is_script_token,
    settings_files,
    split_command,
)


def collect(
    home: Path | None = None,
    registry: dict | None = None,
    patterns: list[str] | None = None,
) -> tuple[list[LinkStatus], dict]:
    """(plan statuses, registry) — the two reads every view shares."""
    home = home or Path.home()
    if registry is None:
        registry = load_registry(home)
    return plan_hooks(home, patterns=patterns, registry=registry), registry


def _harness_names(root: Path, registry: dict) -> list[str]:
    """Every harness a settings file could belong to.

    Hub dirs plus the fallback tables plus registered names: a harness with
    no hooks root and no hub dir (codex today) still declares hooks worth
    drift-checking. The hub's reserved dirs (shared/, _runtime/) are
    machinery, not harnesses.
    """
    names = set(registry) | set(HOOK_FALLBACK) | set(SETTINGS_FALLBACK)
    if root.is_dir():
        names |= {
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and d.name not in RESERVED_HUB_DIRS
        }
    return sorted(names)


def declarations_by_harness(
    home: Path, registry: dict
) -> dict[str, list[Declaration]]:
    root = _paths.hooks_dir_for(home)
    return {
        name: harness_declarations(registry, name, home)
        for name in _harness_names(root, registry)
    }


def _decl_matches(decl: Declaration, script: str) -> bool:
    """The command invokes ``script``.

    Basename for file hooks; a directory hook is invoked through paths
    inside it (``hooks/bundle/guard.py``), so its own name matches as a
    path segment too.
    """
    for raw in split_command(decl.command):
        token = raw.strip("'\"").rstrip(";")
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base == script:
            return True
        if f"/{script}/" in token or token.startswith(f"{script}/"):
            return True
    return False


def _decl_matches_row(
    decl: Declaration, dest: Path, script: str, home: Path
) -> bool:
    """Path-aware match of a declaration to one row's own destination.

    A command that carries a resolvable path must point at THIS row's
    dest: ``~/.claude/hooks/gate.py`` names the harness hook, not the
    shared wrapper ``_generated/gate.py`` — basename matching alone lets
    one declaration claim both tiers, hiding an undeclared one. Only a
    command with no resolvable path at all (a bare ``gate.py``) falls
    back to the basename.
    """
    has_path = any(
        expand_command_path(token, home) is not None
        for token in split_command(decl.command))
    if has_path:
        return _decl_targets(decl, dest, home)
    return _decl_matches(decl, script)


def hook_rows(
    statuses: list[LinkStatus],
    declared: dict[str, list[Declaration]],
    home: Path | None = None,
    registry: dict | None = None,
) -> list[HookRow]:
    """One row per plan status, with the declarations naming its basename.

    The match is by basename, deliberately: settings spell the command as
    ``python3 "$HOME/.claude/hooks/guard.py"`` or a bare ``guard.py``, and
    both mean the script's own name. A script in its hub dir's
    ``.disabled`` reads enabled=False regardless of link state.
    """
    from swe.hooks import shared as _shared

    home = home or Path.home()
    registry = registry if registry is not None else {}
    hub = _paths.hooks_dir_for(home)
    rows: list[HookRow] = []
    for status in statuses:
        if status.source is not None and \
                status.source.name == _shared.GENERATED_DIRNAME:
            # The _generated dir links whole — machinery, not a hook.
            # shared_rows reports its link state per wrapper script, so
            # hiding it here also keeps `delete _generated` unreachable.
            continue
        source = status.source or status.path
        decls = [
            d for d in declared.get(status.label, [])
            if _decl_matches_row(d, status.path, source.name, home)
        ]
        rows.append(HookRow(
            script=source.name,
            harness=status.label,
            source=source,
            dest=status.path,
            state=status.state,
            detail=status.detail,
            declared=decls,
            tier="harness",
            enabled=status.detail != DISABLED_DETAIL,
            events_supported=contract_for(
                registry, status.label, home).normalized_events(),
        ))
    return rows


def shared_rows(
    home: Path,
    registry: dict,
    statuses: list[LinkStatus],
    declared: dict[str, list[Declaration]],
) -> list[HookRow]:
    """One row per shared script x deploy target, plus dormant scripts.

    A deployed wrapper lands at ``<root>/_generated/<name>``; its link
    state is the ``_generated`` directory's own plan status, since the
    dir links whole. A script no installed shim-able harness covers gets
    one row with harness="shared" so it still shows up in the list —
    harness="shared" cannot collide with a real harness dir, the name is
    reserved.
    """
    from swe.hooks import shared as _shared

    gen_status = {
        s.label: s for s in statuses
        if s.source is not None and s.source.name == _shared.GENERATED_DIRNAME
    }
    targets = _shared.deploy_targets(
        home, registry, include_disabled=True)
    disabled = _shared.disabled_names(_shared.shared_dir(home))
    rows: list[HookRow] = []
    covered_scripts: set[str] = set()
    for target in targets:
        covered_scripts.add(target.source.name)
        enabled = target.source.name not in disabled
        root, _why = hook_root(registry, target.harness, home)
        dest = (root / _shared.GENERATED_DIRNAME / target.source.name
                if root is not None else target.wrapper)
        status = gen_status.get(target.harness)
        if not enabled:
            state, detail = "ignored", DISABLED_DETAIL
        elif not target.wrapper.exists():
            state, detail = "create", "wrapper pending — swe hooks sync"
        elif status is not None:
            state, detail = status.state, status.detail
        else:
            state, detail = "linked", ""
        events = _shared.script_events(target.source)
        decls = [
            d for d in declared.get(target.harness, [])
            if _decl_matches_row(d, dest, target.source.name, home)
        ]
        rows.append(HookRow(
            script=target.source.name,
            harness=target.harness,
            source=target.source,
            dest=dest,
            state=state,
            detail=detail,
            declared=decls,
            tier="shared",
            enabled=enabled,
            events_supported=events,
        ))
    for script in _shared.shared_scripts(home):
        if script.name in covered_scripts:
            continue
        events = _shared.script_events(script)
        if script.name in disabled:
            state, detail = "ignored", DISABLED_DETAIL
        elif not events:
            detail = "no events: header — nothing declares it"
            state = "skipped"
        else:
            detail = "declared events cover no installed shim-able harness"
            state = "skipped"
        rows.append(HookRow(
            script=script.name,
            harness="shared",
            source=script,
            dest=script,
            state=state,
            detail=detail,
            declared=[],
            tier="shared",
            enabled=script.name not in disabled,
            events_supported=events,
        ))
    return rows


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _managed_dest(entry: Path, hub: Path) -> bool:
    """Whether a hooks-root entry resolves into the hub (ours, claimed)."""
    return entry.is_symlink() and _inside(entry, hub)


def _decl_targets(decl: Declaration, path: Path, home: Path) -> bool:
    """Whether the declaration's command resolves to ``path``.

    A directory hook is invoked through files inside it, so a path under
    ``path`` counts when ``path`` is itself a directory.
    """
    for token in split_command(decl.command):
        expanded = expand_command_path(token, home)
        if expanded is None:
            continue
        if expanded == path \
                or (path.is_dir() and _inside(expanded, path)):
            return True
    return False


# Shell words that can sit before the script a hook command runs.
# ``python3 /x/gate.py /x/policy.json`` runs gate.py — policy.json is
# argv, not a hook, so only the first path of each command segment rows.
_COMMAND_POSITION_RE = re.compile(
    r"(?:python|pypy|node|deno|bun|ruby|perl|php|tsx|ts-node)[\d.]*"
    r"|(?:ba)?sh|zsh|ksh|dash|fish|csh|tcsh|osascript|pwsh?"
    r"|env|nice|nohup|timeout|xargs|command|exec|uv|pipx?")
# Shells whose ``-c`` argument is a whole script to read for the hook.
_SHELL_RE = re.compile(r"(?:ba)?sh|zsh|ksh|dash")
# ``-c``, ``-lc``, ``-ec``: any short-flag cluster carrying c.
_INLINE_FLAG_RE = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SEGMENT_BREAKS = frozenset({"&&", "||", "|", "|&", ";"})
# Reserved words that open a segment without being its command.
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "while", "until", "do", "!", "{", "(",
})
# Words that run the word after them (past their own flags) as the
# command: ``env MODE=x test -f y`` is a test.
_WRAPPERS = frozenset({"env", "nice", "nohup", "timeout", "command", "exec"})
# A segment whose command is a test reads paths, it runs none of them:
# ``[ -f /x/hook.sh ] && /bin/sh /x/hook.sh`` executes only the second.
_TEST_COMMANDS = frozenset({"[", "[[", "test"})


def _command_lines(command: str) -> list[str]:
    """``command`` cut at unquoted newlines, each line its own command.

    shlex folds a newline into plain whitespace, which would merge a
    ``test -f x`` line and the hook on the next line into one segment.
    """
    lines: list[str] = []
    start, quote, i = 0, "", 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            lines.append(command[start:i])
            start = i + 1
        i += 1
    lines.append(command[start:])
    return lines


def _executed_scripts(
    command: str, home: Path, depth: int = 0, must_exist: bool = True,
    variables: dict[str, str] | None = None,
) -> list[Path]:
    """The file a hook command actually runs — one per shell segment.

    Command-position words (interpreters, env wrappers, VAR=value
    assignments) never name the hook, and neither do a script's own file
    arguments: the first resolvable path per ``&&``/``|``/``;``/newline
    segment is the one. ``sh gate.py config.json`` rows only gate.py. A
    test segment (``[ -f x ]``) runs nothing, and ``sh -c '<script>'`` is
    read for the script its body runs, never as the shell binary itself.
    ``must_exist=False`` (``hooks/identity.py``, naming a past run) keeps a
    path whose file is gone: it is still the script that ran.
    ``variables`` go to ``expand_command_path`` for each parsed token.
    """
    scripts: list[Path] = []
    for line in _command_lines(command):
        # (token, raw): the raw word keeps a -c body's own quoting intact.
        segment: list[tuple[str, str]] = []
        for raw in split_command(line):
            token = raw.strip("'\"")
            ends = token.endswith(";")
            token = token.rstrip(";")
            if token and token not in _SEGMENT_BREAKS:
                segment.append((token, raw))
            if ends or not token or token in _SEGMENT_BREAKS:
                scripts.extend(_segment_scripts(segment, home, depth,
                                                must_exist, variables))
                segment = []
        scripts.extend(_segment_scripts(segment, home, depth, must_exist,
                                        variables))
    return scripts


def _command_start(words: list[str]) -> int:
    """Index of a segment's command: past keywords, VAR=value, wrappers."""
    i = 0
    while i < len(words):
        word = words[i]
        if word in _SHELL_KEYWORDS or _ASSIGNMENT_RE.fullmatch(word):
            i += 1
        elif word.rsplit("/", 1)[-1] in _WRAPPERS:
            i += 1
            # The wrapper's own flags and operands: env -i, timeout 5.
            while i < len(words) and (
                    words[i].startswith("-") or words[i].isdigit()
                    or _ASSIGNMENT_RE.fullmatch(words[i])):
                i += 1
        else:
            break
    return i


def _segment_scripts(
    segment: list[tuple[str, str]], home: Path, depth: int,
    must_exist: bool = True, variables: dict[str, str] | None = None,
) -> list[Path]:
    """What one segment runs: its first path, or what its ``-c`` body runs."""
    words = [token for token, _raw in segment]
    start = _command_start(words)
    if start < len(words) and words[start] in _TEST_COMMANDS:
        return []
    for i in range(start, len(words) - 2):
        base = words[i].rsplit("/", 1)[-1]
        if _SHELL_RE.fullmatch(base) \
                and _INLINE_FLAG_RE.fullmatch(words[i + 1]):
            # Two levels covers sh -c 'bash -c ...'; deeper is noise.
            if depth >= 2:
                return []
            return _executed_scripts(segment[i + 2][1], home, depth + 1,
                                     must_exist, variables)
        if not (_COMMAND_POSITION_RE.fullmatch(base)
                or _ASSIGNMENT_RE.fullmatch(words[i])):
            break
    first = _first_path(words[start:], home, must_exist, variables)
    return [first] if first is not None else []


def _first_path(segment: list[str], home: Path, must_exist: bool = True,
                variables: dict[str, str] | None = None) -> Path | None:
    """The first existing file a command segment runs, if any (with
    ``must_exist=False``, the first path that is not a folder)."""
    for token in segment:
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if _COMMAND_POSITION_RE.fullmatch(base) \
                or _ASSIGNMENT_RE.fullmatch(token):
            continue
        path = expand_command_path(token, home, variables)
        if path is not None:
            # The executed file: later tokens are its arguments, so the
            # first resolvable path wins even when it is missing (a
            # vanished script is drift, not a different hook).
            if must_exist:
                return path if path.is_file() else None
            return None if path.is_dir() else path
    return None


def foreign_rows(
    home: Path,
    registry: dict,
    statuses: list[LinkStatus],
    declared: dict[str, list[Declaration]],
) -> list[HookRow]:
    """Hooks no ~/.swe source claims: the third tier.

    Two sightings, de-duped by (harness, resolved path): a file in a
    hooks root that is not a link into the hub and shares no name with
    a hub source (a dir only when a declaration runs something inside
    it), and the script a settings-declared command executes
    when it resolves outside ~/.swe (its file arguments are not
    hooks). Both keep their real path as source and dest — swe manages
    neither.
    """
    hub = _paths.hooks_dir_for(home)
    swe = _paths.swe_dir_for(home)
    claimed: dict[str, set[str]] = {}
    for status in statuses:
        if status.source is not None:
            claimed.setdefault(status.label, set()).add(status.source.name)
    rows: list[HookRow] = []
    # (harness, resolved path) — the same foreign script declared by two
    # harnesses is two rows, one each, so a scoped view finds its own.
    seen: set[tuple[str, Path]] = set()
    for name in _harness_names(hub, registry):
        contract = contract_for(registry, name, home)
        events = contract.normalized_events()
        root, _why = hook_root(registry, name, home)
        decls = declared.get(name, [])
        runs = [path for decl in decls
                for path in _executed_scripts(decl.command, home)]
        if root is not None and root.is_dir() \
                and not _inside(root, swe):
            for entry in sorted(root.iterdir()):
                if entry.name.startswith(".") \
                        or entry.name in ("__pycache__", "node_modules"):
                    continue
                if entry.name in claimed.get(name, set()):
                    continue
                if _managed_dest(entry, hub):
                    continue
                if entry.is_dir() and not any(
                        _inside(path, entry) for path in runs):
                    # A dir nothing runs from is the harness's own state
                    # (cursor keeps hooks/state/), not a hook — even when
                    # a command passes a file in it as an argument.
                    continue
                key = (name, entry.resolve())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(HookRow(
                    script=entry.name,
                    harness=name,
                    source=entry,
                    dest=entry,
                    state="foreign",
                    detail="no ~/.swe source claims it",
                    declared=[d for d in decls
                              if _decl_matches_row(
                                  d, entry, entry.name, home)],
                    tier="foreign",
                    enabled=True,
                    events_supported=events,
                ))
        for decl in decls:
            for path in _executed_scripts(decl.command, home):
                key = (name, path.resolve())
                if key in seen or _inside(path.resolve(), swe):
                    continue
                if root is not None and _inside(path.resolve(), root):
                    continue  # the dir scan above already rows it
                seen.add(key)
                rows.append(HookRow(
                    script=path.name,
                    harness=name,
                    source=path,
                    dest=path,
                    state="foreign",
                    detail=f"declared in {decl.settings.name} outside "
                           f"~/.swe",
                    declared=[d for d in decls
                              if _decl_targets(d, path, home)
                              or _decl_matches(d, path.name)],
                    tier="foreign",
                    enabled=True,
                    events_supported=events,
                ))
    return rows


def all_rows(
    home: Path,
    registry: dict,
    statuses: list[LinkStatus],
    declared: dict[str, list[Declaration]],
) -> list[HookRow]:
    """The full inventory: harness rows, then shared, then foreign."""
    return (
        hook_rows(statuses, declared, home, registry)
        + shared_rows(home, registry, statuses, declared)
        + foreign_rows(home, registry, statuses, declared)
    )


def find_script(name: str, rows: list[HookRow]) -> list[HookRow]:
    """Rows for ``name``: exact filename first, then every stem match.

    ``edit prose-guard`` finds prose-guard.py; ``edit guard`` finds both
    guard.py and guard.sh so the caller can ask which harness is meant
    rather than silently picking the .py.
    """
    exact = [r for r in rows if r.script == name]
    if exact:
        return exact
    return [r for r in rows if Path(r.script).stem == name]


# How much of a script ``script_summary`` reads looking for its header.
_SUMMARY_HEAD = 16 * 1024

# A module docstring at the top of a Python head too long to parse whole.
_LEADING_DOCSTRING = re.compile(
    r'(?:[ \t]*(?:#[^\n]*)?\n)*[ \t]*[rRuU]?("""|\'\'\')(.*?)\1', re.DOTALL)


def script_summary(path: Path, limit: int = 2) -> str:
    """The script's own description: docstring or leading comment lines.

    A hook carries no metadata, so its contract lives in its header —
    prose-guard.py's docstring states the exit-0/2 contract and the
    fail-open rule. ``limit`` lines, stopping at the first blank.
    """
    # The header sits at the top: read a bounded head, never the whole
    # file. A foreign "script" can be a large binary (Agent Relay Helper),
    # and the list reads one per row on every refresh.
    try:
        with path.open("rb") as handle:
            head = handle.read(_SUMMARY_HEAD + 1)
    except OSError:
        return ""
    if b"\0" in head:
        return ""
    whole = len(head) <= _SUMMARY_HEAD
    text = head[:_SUMMARY_HEAD].decode("utf-8", errors="replace")
    lines: list[str] = []
    if path.suffix == ".py":
        doc = None
        if whole:
            try:
                doc = ast.get_docstring(ast.parse(text))
            except (SyntaxError, ValueError, RecursionError):
                doc = None
        else:
            # A cut-off head won't parse; the docstring opens the module,
            # after any shebang, coding line or comments.
            match = _LEADING_DOCSTRING.match(text)
            doc = match.group(2).strip() if match else None
        if doc:
            for line in doc.splitlines():
                if not line.strip():
                    break
                lines.append(line.strip())
    else:
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#!"):
                continue
            if not line.startswith("#"):
                break
            content = line.lstrip("#").strip()
            if not content:
                break
            lines.append(content)
        if len(lines) >= limit:
            lines = lines[:limit]
    return " ".join(lines[:limit])


# --- drift findings --------------------------------------------------------


def _token_verdict(token: str, home: Path, candidates: list[Path]) -> str:
    """"resolved" | "missing" | "unverifiable" for one script-looking token."""
    bare = token.strip("'\"").rstrip(";")
    base = bare.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "/" not in bare and "\\" not in bare:
        # A bare basename: the hooks root, the hub dir and the settings
        # file's own hooks/ sibling are the homes it could mean.
        if any((d / base).exists() for d in candidates):
            return "resolved"
        return "missing"
    path = expand_command_path(bare, home)
    if path is None:
        return "unverifiable"
    if path.exists():
        return "resolved"
    # The token names a path inside a hooks dir swe knows about (the root,
    # the hub, the settings file's sibling hooks/): a missing file there is
    # the not-yet-linked case when the same basename exists in any of them.
    # Outside those dirs a dead literal path is simply missing — a
    # same-named file under ~/.swe/hooks is not the script it means.
    if path.parent in candidates and any(
            (d / base).exists() for d in candidates):
        return "resolved"
    return "missing"


def _declared_missing(
    home: Path,
    registry: dict,
    statuses: list[LinkStatus],
    declared: dict[str, list[Declaration]],
) -> list[HookDrift]:
    """(a) A declaration whose script exists nowhere swe can find it.

    One finding per (harness, basename): a script invoked from six events
    is still one missing script. Installer-owned scripts live only at the
    destination, so a basename counts as present when it exists in *any*
    candidate dir — only pointing at nothing at all is drift.
    """
    root_dir = _paths.hooks_dir_for(home)
    dest_roots = {s.label: s.path.parent for s in statuses}
    # A directory hook is invoked through paths inside it, so its hub and
    # destination dirs are candidate homes for those child paths too. A
    # hub dir that names no harness is no one's hook dir: a bare basename
    # matching a file in it is not the script another harness means.
    dir_homes = [p for s in statuses
                 if s.source is not None and s.source.is_dir()
                 and s.detail != UNKNOWN_HARNESS_DETAIL
                 for p in (s.source, s.path)]
    out: list[HookDrift] = []
    for name, decls in declared.items():
        root, _why = hook_root(registry, name, home)
        candidates = ([d for d in (root, dest_roots.get(name),
                                 root_dir / name)
                       if d is not None] + dir_homes)
        # basename -> (the event labels, the settings files, the missing
        # token keys) declaring it, for one merged finding. The key is
        # the resolved path when the token expands, else the token as
        # written: one basename can be missing in one command and
        # present in another, so the drop matches tokens.
        missing: dict[str, tuple[set[str], set[Path], set[str]]] = {}
        for decl in decls:
            toks = split_command(decl.command)
            homes = candidates + [decl.settings.parent / "hooks"]
            tokens = [t for t in toks if is_script_token(t)]
            if not tokens:
                continue
            # `[ -f p.sh ] && p.sh` handles its own absence: a tested path
            # being missing is the command working as written, not drift.
            guarded_bases: set[str] = set()
            guarded_paths: set[Path] = set()
            for i, t in enumerate(toks[:-1]):
                if t in ("-f", "-x"):
                    probe = toks[i + 1].strip("'\"").rstrip(";")
                    guarded_bases.add(probe.rsplit("/", 1)[-1])
                    expanded = expand_command_path(probe, home)
                    if expanded is not None:
                        guarded_paths.add(expanded)
            for token in tokens:
                if _token_verdict(token, home, homes) != "missing":
                    continue
                bare = token.strip("'\"").rstrip(";")
                base = bare.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                expanded = expand_command_path(bare, home)
                if base in guarded_bases or (
                        expanded is not None and expanded in guarded_paths):
                    continue
                labels, files, keys = missing.setdefault(
                    base, (set(), set(), set()))
                labels.add(decl.label())
                files.add(decl.settings)
                keys.add(str(expanded) if expanded is not None else bare)
        for base, (labels, files, keys) in sorted(missing.items()):
            where = ", ".join(sorted(labels))
            out.append(HookDrift(
                "declared-missing", name,
                f"declared {base} ({where}) but no such script is on disk",
                script=base, settings=tuple(sorted(files)),
                targets=tuple(sorted(keys)),
            ))
    return out


def _undeclared(
    rows: list[HookRow], home: Path, registry: dict
) -> list[HookDrift]:
    """(b) A hub script linked into place that nothing ever invokes.

    Shared rows count too: a generated wrapper no declaration reaches
    never fires. Foreign rows do not — a script swe does not own sitting
    undeclared is the machine's business, not drift. Nor does the
    ``_generated`` dir link itself — a declaration names the script
    inside it, so the shared rows already carry that signal and warning
    on the directory would only double the noise.
    """
    from swe.hooks import shared as _shared
    from swe.hooks.settings import configured_settings

    out: list[HookDrift] = []
    for row in rows:
        if not (row.state == "linked" and not row.declared
                and row.tier in ("harness", "shared") and row.enabled
                and not (row.tier == "harness"
                         and row.script == _shared.GENERATED_DIRNAME
                         and row.source.is_dir())):
            continue
        # The file a declaration would go in: the capability's own list
        # first, else the files swe already reads for this harness.
        files = (configured_settings(registry, row.harness, home)
                 or settings_files(registry, row.harness, home))
        out.append(HookDrift(
            "undeclared", row.harness,
            f"{row.script} is linked into {row.dest.parent} but no settings "
            f"file declares it — the hook will not fire",
            script=row.script, settings=tuple(files),
        ))
    return out


def _managed_settings(
    home: Path, registry: dict
) -> list[HookDrift]:
    """(d) A declaration file swe must not pretend is editable.

    kind="managed" is a note, not drift: a symlinked or read-only
    settings file is owned by whatever put it there (nix, dotfiles), so
    check reports "declaration managed elsewhere" rather than implying
    the user should write it. ``swe hooks new`` repeats the same note.
    """
    out: list[HookDrift] = []
    root = _paths.hooks_dir_for(home)
    for name in _harness_names(root, registry):
        for path in settings_files(registry, name, home):
            if declaration_managed_elsewhere(path):
                out.append(HookDrift(
                    "managed", name,
                    f"{path} — declaration managed elsewhere",
                ))
    return out


def _unlinked(rows: list[HookRow]) -> list[HookDrift]:
    """(c) A hub script the destination never received (or cannot receive).

    ``skipped`` and ``ignored`` stay quiet: a harness that is not installed
    and a path in .linkignore are deliberate, not drift.
    """
    out: list[HookDrift] = []
    for row in rows:
        if row.state == "create":
            out.append(HookDrift(
                "unlinked", row.harness,
                f"{row.script} is not linked into {row.dest.parent} "
                f"(swe init will link it)",
                script=row.script,
            ))
        elif row.state == "relink":
            out.append(HookDrift(
                "unlinked", row.harness,
                f"{row.script}'s link points elsewhere ({row.detail})",
                script=row.script,
            ))
        elif row.state == "absorb":
            out.append(HookDrift(
                "unlinked", row.harness,
                f"{row.script} sits at {row.dest.parent} as an identical "
                f"copy, not a link (swe init will absorb it)",
                script=row.script,
            ))
        elif row.state == "conflict":
            out.append(HookDrift(
                "unlinked", row.harness,
                f"{row.script} is blocked by a real file at {row.dest} "
                f"(swe init --force replaces it)",
                script=row.script,
            ))
    return out


def hook_findings(
    home: Path | None = None,
    registry: dict | None = None,
    patterns: list[str] | None = None,
) -> list[HookDrift]:
    """The ``swe hooks check`` findings: (a), (b), (c), then managed notes."""
    home = home or Path.home()
    statuses, registry = collect(home, registry, patterns)
    declared = declarations_by_harness(home, registry)
    rows = all_rows(home, registry, statuses, declared)
    return (
        _declared_missing(home, registry, statuses, declared)
        + _undeclared(rows, home, registry)
        + _unlinked(rows)
        + _managed_settings(home, registry)
    )
