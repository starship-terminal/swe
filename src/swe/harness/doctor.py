"""Assemble ``swe doctor`` findings from the path-health probes."""

from __future__ import annotations

import json
from pathlib import Path

from swe.console import tilde
from swe.harness.models import DoctorFinding
from swe.harness.path_health import is_dir_on_path


def collect_findings(env, preferred, which_npm, nvm_dirs, orphans, drift) -> list:
    """Every problem ``swe doctor`` knows about, as finding models.

    Severity ``error`` is exactly what makes the exit status 1: an off-PATH
    harness, an npm global bin missing from PATH, or an error-level drift
    finding. A missing node or npm, nvm bins off PATH and drift warnings
    are warnings, and a differing preferred npm is info, so ``ok`` and the
    exit status always agree.
    """
    findings = []
    if not env.node:
        findings.append(DoctorFinding(
            "node", "warning", "node not found on PATH"))
    if not env.npm:
        findings.append(DoctorFinding(
            "npm", "warning", "npm not found on PATH",
            "Install Node/npm, or put npm on PATH."))
    if env.global_bin and not env.global_bin_on_path:
        findings.append(DoctorFinding(
            "npm.global-bin", "error",
            f"npm global bin {env.global_bin} is not on PATH",
            f'export PATH="{env.global_bin}:$PATH"'))
    if preferred and which_npm:
        try:
            differs = (Path(preferred).resolve()
                       != Path(which_npm).resolve())
        except (OSError, RuntimeError):
            # A symlink loop or hostile PATH entry must not kill the
            # report — compare the raw strings instead.
            differs = preferred != which_npm
        if differs:
            which_dir = str(Path(which_npm).parent)
            preferred_dir = str(Path(preferred).parent)
            # Removing the PATH's npm is only safe when the preferred one
            # is already reachable; otherwise it is the only npm there is.
            preferred_on_path = is_dir_on_path(Path(preferred_dir))
            # A shell-init edit does not reach the process already
            # serving the web app: the check clears on the next hub start.
            note = " A shell edit only counts from a new shell or a hub restart."
            if preferred_on_path:
                fix = (f"reorder PATH so {preferred_dir} wins, or remove "
                       f"the npm at {which_npm}")
                steps = ("Put {0} ahead of {1}, or remove npm from {1}."
                         .format(preferred_dir, which_dir) + note)
                prompt = (f"`which npm` resolves to {which_npm} but swe "
                          f"prefers {preferred}. Inspect the shell init "
                          f"files and reorder PATH so the preferred npm "
                          f"wins, or remove npm from {which_dir}. Verify "
                          f"with `swe doctor` from a new shell: the "
                          f"npm.preferred finding should be gone.")
            else:
                fix = f"add {preferred_dir} to PATH ahead of {which_dir}"
                steps = (f"{preferred_dir} is not on PATH, so removing "
                         f"{which_dir}'s npm would leave none. Add "
                         f"{preferred_dir} to PATH in your shell init."
                         + note)
                prompt = (f"`which npm` resolves to {which_npm} but swe "
                          f"prefers {preferred}, and {preferred_dir} is "
                          f"not on PATH at all. Inspect the shell init "
                          f"files and add {preferred_dir} to PATH. Do "
                          f"NOT remove {which_dir}'s npm: it is the only "
                          f"one on PATH. Verify with `swe doctor` from a "
                          f"new shell: the npm.preferred finding should "
                          f"be gone.")
            findings.append(DoctorFinding(
                "npm.preferred", "info",
                f"which npm is {which_npm}; swe prefers {preferred}",
                fix,
                source=f"PATH order: {which_dir} precedes {preferred_dir}",
                remedies=(
                    {"kind": "steps",
                     "title": "Reorder PATH in your shell init",
                     "detail": steps},
                    {"kind": "agent", "title": "Fix with agent",
                     "detail": "An agent inspects your shell init files "
                              "and makes the edit itself.",
                     "prompt": prompt},
                )))
    if nvm_dirs and not any(is_dir_on_path(d) for d in nvm_dirs):
        findings.append(DoctorFinding(
            "nvm", "warning",
            "nvm node bins are not on PATH, so nvm globals are invisible "
            "to swe and non-interactive shells",
            "Avoid `npm install -g` under nvm unless nvm is always on PATH. "
            "Prefer: swe install <name>"))
    for name, command, hit in orphans:
        via = install_method(name)
        fix = (f"swe install {name} --via={via}" if via
               else f"swe install {name}")
        findings.append(DoctorFinding(
            "off-path", "error",
            f"{name} ({command}) is at {hit.path} [{hit.source}], not on PATH",
            fix,
            harness=name, action="install" if via else None, via=via,
            source=f"found at {hit.path} [{hit.source}]",
            remedies=({"kind": "action", "action": "install", "via": via,
                       "title": "Reinstall with swe",
                       "command": fix,
                       "detail": f"{fix}: reinstalls the way the stray "
                                f"copy was installed, so it lands on PATH."},)
            if via else
            ({"kind": "steps", "title": "Put it on PATH yourself",
              "detail": fix},)))
    for finding in drift:
        findings.append(DoctorFinding(
            f"drift.{finding.area}",
            "error" if finding.severity == "error" else "warning",
            finding.message, finding.fix,
            harness=finding.harness, action=finding.action,
            source=finding.source, remedies=finding.remedies))
    return findings


def install_method(name: str) -> str | None:
    """The method a Run reinstalls an off-PATH harness with: the one that
    put the stray copy there (an nvm npm install comes back through the
    npm on PATH), else the first whose tool is on PATH. None when no
    installer's tool is on PATH, so there is nothing to run."""
    import os
    import shutil

    from swe.harness.commands import _install_command_name
    from swe.harness.installers import METHOD_TOOL, installers_for, uninstaller_for
    from swe.harness.path_health import search_dirs_for_command
    from swe.harness.registry import load_registry_readonly

    usable = [inst.via for inst in installers_for(name) if shutil.which(METHOD_TOOL[inst.via])]
    if not usable:
        return None
    command = _install_command_name(name, load_registry_readonly())
    for hit in search_dirs_for_command(command):
        if hit.source == "path":
            continue
        remover = uninstaller_for(name, os.path.realpath(hit.path))
        if remover is not None and remover.via in usable:
            return remover.via
    return usable[0]


def issue_findings(home: Path | None = None) -> list[DoctorFinding]:
    """Usage problems the app recorded in ``~/.swe/data/issues.json``.

    The app's IssueStore is the source of truth for "this needs the
    user"; each entry becomes a finding whose fix text comes from the
    REMEDIES table, so ``swe doctor`` and the app describe the same fix.
    All of them are warnings or info — a stale sign-in must never fail
    doctor's exit status. An outage is listed only once it has failed
    ``PERSISTENT_POLLS`` polls in a row; a single blip stays on its card.
    """
    from swe import paths
    from swe.harness.remedies import (
        AUTH_REQUIRED,
        PERSISTENT_POLLS,
        SIGNED_OUT,
        remedy_for,
    )

    try:
        raw = json.loads(paths.issues_file_for(home or Path.home()).read_text())
    except (OSError, ValueError):
        return []
    issues = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(issues, dict):
        return []
    findings: list[DoctorFinding] = []
    for issue_id, issue in sorted(issues.items()):
        if not isinstance(issue, dict) or issue.get("resolved"):
            continue
        name = issue.get("harness")
        status = issue.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        outage = status in ("unavailable", "timeout")
        occurrences = issue.get("occurrences")
        if outage and not (isinstance(occurrences, int)
                           and occurrences >= PERSISTENT_POLLS):
            continue
        stored = issue.get("remedy") if isinstance(issue.get("remedy"), dict) else None
        # The table is authoritative — it may have learned a better fix
        # since the app wrote the issue. The one exception is an extra
        # account's login fix: ``remedy_for_account`` aimed it at that
        # account's own files, which this file can't redo without the
        # account's directory. Every other status has the same fix for
        # every account, so the table wins there too.
        account_login = (bool(issue.get("accountKey") or issue.get("account"))
                         and status in (SIGNED_OUT, AUTH_REQUIRED))
        remedy = ((stored if account_login else None)
                  or remedy_for(name, status) or stored or {})
        summary = remedy.get("summary") or (stored or {}).get("summary")
        message = summary or f"{name}: {status}"
        account = issue.get("account")
        if isinstance(account, str) and account:
            message = f"{message} ({account})"
        fix = remedy.get("command") or remedy.get("url")
        if fix is None:
            steps = remedy.get("steps")
            if isinstance(steps, list) and steps:
                fix = steps[0]
        severity = "info" if outage else "warning"
        findings.append(DoctorFinding(
            f"usage.{issue_id}", severity, message, fix, harness=name))
    return findings


def mirror_audit(home: Path | None = None) -> dict | None:
    """The transcript mirror's state for ``swe doctor``
    (``transcript_mirror.audit``), or None when it cannot be read."""
    from swe.sessions import transcript_mirror

    try:
        return transcript_mirror.audit(home or Path.home())
    except Exception:  # bad mirror state is never a doctor failure
        return None


def _num(value) -> int:
    """A count from ``mirror.json`` or the manifest, which a person or an
    older swe may have left in any shape: 0 for anything not a number."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value == value and abs(value) != float("inf") else 0
    return 0


def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def _count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n:,} {noun if n == 1 else (plural or noun + 's')}"


def mirror_lines(audit: dict | None) -> list[tuple[str, str]]:
    """``(label, value)`` rows for the human ``swe doctor`` section."""
    if audit is None:
        return [("state", "unreadable")]
    root = tilde(audit.get("root") or "")
    rows = [("state", f"{'on' if audit.get('enabled') else 'off'}  ({root})"),
            ("copies", f"{_count(_num(audit.get('entries')), 'copy', 'copies')} "
                       f"({_size(_num(audit.get('bytes')))})"),
            ("orphans", f"{_count(_num(audit.get('orphaned')), 'orphan')} "
                        f"({_size(_num(audit.get('orphaned_bytes')))}), kept after "
                        "the harness deleted the source")]
    last = audit.get("last_pass")
    if isinstance(last, dict) and last:
        import time

        finished = _num(last.get("finished_at"))
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(finished)) if finished > 0
                else "at an unknown time")
        made = _num(last.get("copied")) + _num(last.get("cloned"))
        rows.append(("last pass", f"{when} · {_count(_num(last.get('files')), 'file')}, "
                                  f"{made:,} copied, {_num(last.get('failed')):,} failed"))
    else:
        rows.append(("last pass", "none yet"))
    return rows


def mirror_findings(audit: dict | None) -> list[DoctorFinding]:
    """What ``swe doctor`` flags about the transcript mirror. Never an
    error: the mirror's state cannot fail doctor's exit status."""
    if audit is None:
        return []
    source = tilde(audit.get("root") or "~/.swe/data/transcripts")
    findings: list[DoctorFinding] = []
    if not audit.get("enabled"):
        findings.append(DoctorFinding(
            "mirror.off", "info",
            "The transcript mirror is off, so Claude Code and Codex transcripts are lost "
            "when the harness prunes them",
            "swe session mirror --on", source=source))
    last = audit.get("last_pass")
    failed = _num(last.get("failed")) if isinstance(last, dict) else 0
    if failed:
        findings.append(DoctorFinding(
            "mirror.failed", "warning",
            f"The last mirror pass could not copy {_count(failed, 'transcript')}",
            "swe session mirror", source=source))
    untracked = audit.get("untracked") if isinstance(audit.get("untracked"), list) else []
    if untracked:
        findings.append(DoctorFinding(
            "mirror.untracked", "info",
            f"{_count(len(untracked), 'mirror copy', 'mirror copies')} not in the manifest: "
            "they list and read, but swe cannot restore or purge them",
            None, source=source))
    unreadable = _num(audit.get("unreadable"))
    if unreadable:
        findings.append(DoctorFinding(
            "mirror.unreadable", "info",
            f"{_count(unreadable, 'manifest entry', 'manifest entries')} could not be read: "
            "swe skips them and leaves their copies alone, and keeps them in the file",
            None, source=source))
    missing = audit.get("missing") if isinstance(audit.get("missing"), list) else []
    if missing:
        findings.append(DoctorFinding(
            "mirror.missing", "info",
            f"{_count(len(missing), 'manifest entry', 'manifest entries')} name a copy that "
            "is no longer on disk",
            "swe session mirror", source=source))
    stale = audit.get("stale_temps") if isinstance(audit.get("stale_temps"), list) else []
    if stale:
        findings.append(DoctorFinding(
            "mirror.stale-tmp", "warning",
            f"{_count(len(stale), 'temp file')} left in the mirror by an interrupted copy",
            "swe session mirror --sweep-tmp", source=source))
    return findings


def setup_findings(home: Path | None = None) -> list[DoctorFinding]:
    """Setup-state checks ``swe doctor`` runs after the path probes.

    They short-circuit so the user fixes the root cause first: no hub, no
    shared AGENTS.md, an empty registry (which makes the per-harness link
    check meaningless). Every one is a warning, so ``ok`` and the exit
    status stay untouched.
    """
    from swe import paths
    from swe.harness.registry import is_active, load_registry_readonly
    from swe.init.layout import link_states

    home = home or Path.home()
    if not paths.swe_dir_for(home).is_dir():
        return [DoctorFinding(
            "setup.hub", "warning",
            "~/.swe doesn't exist yet, so nothing is shared between harnesses",
            "swe setup --apply", action="setup", source="~/.swe",
            remedies=({"kind": "action", "action": "setup",
                       "title": "swe setup --apply",
                       "detail": "Registers the harnesses it finds, imports "
                                "their MCP servers, and links the shared "
                                "AGENTS.md and skills into each."},))]
    if not (paths.swe_dir_for(home) / "AGENTS.md").is_file():
        return [DoctorFinding(
            "setup.agents-md", "warning",
            "No shared AGENTS.md in ~/.swe",
            "swe setup --apply", action="setup", source="~/.swe",
            remedies=({"kind": "action", "action": "setup",
                       "title": "swe setup --apply",
                       "detail": "Registers the harnesses it finds, imports "
                                "their MCP servers, and links the shared "
                                "AGENTS.md and skills into each."},))]

    registry = load_registry_readonly()
    if not registry:
        return [DoctorFinding(
            "setup.registry", "warning",
            "No harnesses registered",
            "swe harness discover --apply", action="discover",
            remedies=({"kind": "action", "action": "discover",
                       "title": "swe harness discover --apply",
                       "detail": "Adds the high-confidence matches it finds "
                                "to harness.json."},))]

    findings: list[DoctorFinding] = []
    states = link_states(home)
    for name, entry in registry.items():
        if not is_active(entry):
            continue
        wanted = []
        per = states.get(name, {})
        if per.get("agents") in ("create", "relink"):
            wanted.append("the shared AGENTS.md")
        if per.get("skills") in ("create", "relink"):
            wanted.append("skills")
        if wanted:
            from swe.harness.models import display_name
            title = display_name(entry) or name
            findings.append(DoctorFinding(
                "setup.unlinked", "warning",
                f"{title} doesn't read {' or '.join(wanted)} yet",
                "swe init", harness=name, action="init",
                remedies=({"kind": "action", "action": "init",
                           "title": "Link it with swe init",
                           "detail": "A real file in the way is left alone "
                                    "and listed for Replace and back up."},)))
    findings.extend(marketplace_findings(home, registry))
    return findings


# The file each plugin-capable harness declares marketplaces in. Claude
# also keeps the ones `claude plugin marketplace add` registered in
# plugins/known_marketplaces.json, which its own remove command edits.
_MARKETPLACE_CONFIG = {"claude": ".claude/settings.json", "codex": ".codex/config.toml"}


def _declared_in_settings(home: Path, harness: str) -> set[str]:
    """Names Claude's settings.json declares."""
    if harness != "claude":
        return set()
    try:
        data = json.loads((home / _MARKETPLACE_CONFIG["claude"]).read_text())
    except (OSError, ValueError):
        return set()
    extra = data.get("extraKnownMarketplaces") if isinstance(data, dict) else None
    return set(extra) if isinstance(extra, dict) else set()


def _cli_registered(home: Path) -> set[str]:
    """Names in claude's known_marketplaces.json, the CLI's register."""
    try:
        data = json.loads((home / _KNOWN_MARKETPLACES).read_text())
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, dict) else set()


def marketplace_findings(home: Path, registry: dict | None = None) -> list[DoctorFinding]:
    """One warning per harness whose registered folder marketplaces are gone.

    A settings file synced from another machine can name
    ``~/.swe/plugins/<name>`` folders that never reached this one (#696).
    Only ``directory`` marketplaces count: a git or GitHub one with no
    manifest yet is fetched by the harness itself. When the settings file
    is a Nix store link, editing it would fail, so the fix says where the
    change belongs instead. A harness whose files could not be read gets a
    warning saying the check did not finish, rather than silence.
    """
    import os

    from swe.harness.models import display_name
    from swe.plugins.catalog import read_views

    def title(name: str) -> str:
        return display_name((registry or {}).get(name) or {}) or name

    errors: list = []
    views = read_views(home, errors)
    findings: list[DoctorFinding] = []
    for name in sorted({e["harness"] for e in errors} - set(views)):
        detail = next(e["detail"] for e in errors if e["harness"] == name)
        findings.append(DoctorFinding(
            "setup.marketplace-unchecked", "warning",
            f"Couldn't check {title(name)}'s marketplaces: {detail}", harness=name))
    for name, view in views.items():
        missing = sorted(m.name for m in view.markets.values()
                         if m.source_type == "directory" and m.root is not None
                         and not m.root.is_dir())
        if not missing:
            continue
        listed = ", ".join(missing)
        declared = _declared_in_settings(home, name) if name == "claude" else set(missing)
        known = _cli_registered(home) if name == "claude" else set()
        # The two registers are independent: a name in both files is in
        # both drop lists, or Run removes half of it and the warning
        # stays. A claude name in neither still defaults to the settings
        # blame, matching the source line's story.
        in_file = [m for m in missing
                   if m in declared or (name == "claude" and m not in known)]
        by_cli = [m for m in missing if m in known]
        config = home / _MARKETPLACE_CONFIG.get(name, "")
        nix = name in _MARKETPLACE_CONFIG and in_file and \
            os.path.realpath(config).startswith("/nix/store/")
        from swe.hooks.settings import declaration_managed_elsewhere
        # A managed file (symlinked into the Nix store, read-only, a
        # dotfiles checkout) is skipped at emit, not just refused at run.
        managed = name in _MARKETPLACE_CONFIG and in_file and \
            declaration_managed_elsewhere(config)
        managed_known = name == "claude" and by_cli and \
            declaration_managed_elsewhere(home / _KNOWN_MARKETPLACES)
        settings_entries = [] if (nix or managed) else in_file
        cli_entries = [] if managed_known else by_cli
        if nix:
            fix = (f"~/{_MARKETPLACE_CONFIG[name]} is managed by Nix: copy the folders "
                   "onto this machine or drop them from your Nix config")
        elif managed:
            fix = (f"~/{_MARKETPLACE_CONFIG[name]} is managed elsewhere: "
                   "copy the folders onto this machine, or drop the entries "
                   "where that file is managed")
        elif name in _MARKETPLACE_CONFIG:
            ways = []
            if in_file:
                ways.append(f"drop {', '.join(in_file)} from ~/{_MARKETPLACE_CONFIG[name]}")
            ways += [f"{name} plugin marketplace remove {m}" for m in by_cli]
            fix = "copy the folders onto this machine, or " + "; ".join(ways)
        else:
            fix = "copy the folders onto this machine"
        findings.append(DoctorFinding(
            "setup.marketplace-missing", "warning",
            f"{title(name)} registers {len(missing)} marketplace folder"
            f"{'s' if len(missing) != 1 else ''} missing on this machine: {listed}",
            fix, harness=name,
            source=_marketplace_source(name, in_file, by_cli),
            remedies=_marketplace_remedies(
                name, missing, in_file, by_cli, nix,
                settings_entries, cli_entries, managed)))
    return findings


# Claude keeps CLI-added marketplaces beside its settings file, in
# plugins/known_marketplaces.json.
_KNOWN_MARKETPLACES = ".claude/plugins/known_marketplaces.json"
_MISSING = object()


def drop_marketplace_entries(
    home: Path, in_file: list[str], by_cli: list[str]
) -> str | None:
    """The deterministic half of a missing-marketplace fix: delete the
    named entries, each file backed up to ~/.swe/backups first.

    Claude only — codex declares marketplaces in TOML, which nothing
    here writes. ``in_file`` names come out of settings.json's
    ``extraKnownMarketplaces`` map, ``by_cli`` out of
    known_marketplaces.json. A managed settings file (symlinked into the
    Nix store, unwritable) is skipped with a note rather than failed, so
    the CLI-registered drops still land. Returns what went wrong.
    """
    from swe.hooks.settings import _unchanged, declaration_managed_elsewhere
    from swe.paths import atomic_write_text, backup_tree

    errors: list[str] = []
    jobs = (
        (home / _MARKETPLACE_CONFIG["claude"], "extraKnownMarketplaces",
         in_file),
        (home / _KNOWN_MARKETPLACES, None, by_cli),
    )
    for path, key, names in jobs:
        if not names:
            continue
        tilde_path = tilde(path, home)
        if declaration_managed_elsewhere(path):
            errors.append(f"{tilde_path} is managed elsewhere; edit it there")
            continue
        try:
            before = path.stat()
        except OSError:
            before = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            errors.append(f"could not read {tilde_path}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{tilde_path} is not a JSON object")
            continue
        table = data.get(key) if key else data
        if not isinstance(table, dict):
            errors.append(f"{tilde_path} has no {key} map")
            continue
        removed = [n for n in names if table.pop(n, _MISSING) is not _MISSING]
        if not removed:
            continue
        try:
            _unchanged(path, before)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        backup_tree(path, home)
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return "; ".join(errors) or None


def _marketplace_source(
    name: str, in_file: list[str], by_cli: list[str]
) -> str | None:
    """The file(s) registering the missing folders, for the source line."""
    files = []
    if name in _MARKETPLACE_CONFIG and in_file:
        files.append(f"~/{_MARKETPLACE_CONFIG[name]}")
    if name == "claude" and by_cli:
        files.append(f"~/{_KNOWN_MARKETPLACES}")
    if not files:
        return None
    return "registered in " + " · ".join(files)


def _marketplace_remedies(
    name: str, missing: list[str], in_file: list[str],
    by_cli: list[str], nix: bool,
    settings_entries: list[str] | None = None,
    cli_entries: list[str] | None = None,
    managed: bool = False,
) -> tuple:
    """Ways out of a missing-marketplace finding, best first.

    Restoring the folders beats dropping the entries, since a synced
    settings file usually means the folders live on the user's other
    machine. An agent stays the escape for mixed keep/repoint/remove
    calls; a Nix-managed settings file narrows the manual side to the
    Nix config itself.
    """
    listed = ", ".join(missing)
    remedies: list[dict] = [{
        "kind": "steps",
        "title": "Copy the folders onto this machine",
        "detail": "The settings came from a machine that has them; "
                  "bring them over and the entries work again.",
    }]
    # CLI-registered entries live in known_marketplaces.json, which Nix
    # does not manage: they come out through the harness's own remove
    # command under either management style. The caller has already
    # emptied each entry list whose file is managed, so a Run only ever
    # names files it can actually edit.
    if name == "claude":
        settings_entries = settings_entries or []
        cli_entries = cli_entries or []
        n_entries = len(settings_entries) + len(cli_entries)
        if n_entries:
            files = []
            if settings_entries:
                files.append(f"~/{_MARKETPLACE_CONFIG[name]}")
            if cli_entries:
                files.append(f"~/{_KNOWN_MARKETPLACES}")
            remedies.append({
                "kind": "action", "action": "marketplace-drop",
                "title": f"Drop the {n_entries} entr"
                         f"{'y' if n_entries == 1 else 'ies'}",
                "detail": f"Deletes them from {' and '.join(files)}, "
                          "with each file backed up to ~/.swe/backups "
                          "first.",
                "settings_entries": settings_entries,
                "cli_entries": cli_entries,
            })
    cli_steps = [f"{name} plugin marketplace remove {m}" for m in by_cli]
    drop_steps = []
    if nix:
        if in_file:
            drop_steps.append(
                f"drop {', '.join(in_file)} from your Nix config "
                f"(~/{_MARKETPLACE_CONFIG[name]} is generated)")
    elif managed:
        if in_file:
            drop_steps.append(
                f"drop {', '.join(in_file)} where "
                f"~/{_MARKETPLACE_CONFIG[name]} is managed "
                "(the file is read-only or generated)")
    else:
        if in_file:
            drop_steps.append(
                f"drop {', '.join(in_file)} from "
                f"~/{_MARKETPLACE_CONFIG.get(name, 'settings')}")
    drop_steps += cli_steps
    if drop_steps:
        remedies.append({
            "kind": "steps",
            "title": f"Or drop the {len(missing)} entr"
                     f"{'y' if len(missing) == 1 else 'ies'}",
            "steps": drop_steps,
        })
    if name in _MARKETPLACE_CONFIG:
        config = f"~/{_MARKETPLACE_CONFIG[name]}"
        places = [config] + ([f"~/{_KNOWN_MARKETPLACES}"]
                             if name == "claude" and by_cli else [])
        if nix:
            # The settings file resolves into the store: its entries go
            # in the Nix config that generates it; CLI-registered ones
            # still leave through the harness's own remove command.
            prompt = (f"{config} is a symlink into /nix/store and "
                      f"registers {len(missing)} marketplace "
                      f"folder{'s' if len(missing) != 1 else ''} missing "
                      f"on this machine ({listed}). Do NOT edit {config}. "
                      + (f"For {', '.join(in_file)}: find the Nix or "
                         "home-manager config that generates the file "
                         "and drop the entries there. " if in_file else "")
                      + (f"For {', '.join(by_cli)}: run `{name} plugin "
                         "marketplace remove <name>` per entry, which "
                         f"edits ~/{_KNOWN_MARKETPLACES}, or remove "
                         "them there by hand. " if by_cli else "")
                      + "Restore a folder instead when it should exist "
                      "here. Verify with `swe doctor`: the "
                      "setup.marketplace-missing finding should be gone.")
        elif managed:
            prompt = (f"{config} registers {len(missing)} marketplace "
                      f"folder{'s' if len(missing) != 1 else ''} missing "
                      f"on this machine ({listed}), but the file is "
                      "managed elsewhere: it is read-only or generated. "
                      "Do NOT edit it directly. Find what manages it "
                      "(a dotfiles repo, a Nix or home-manager config) "
                      f"and drop {', '.join(in_file)} there. "
                      + (f"For {', '.join(by_cli)}: run `{name} plugin "
                         "marketplace remove <name>` per entry. " if by_cli else "")
                      + "Restore a folder instead when it should exist "
                      "here. Verify with `swe doctor`: the "
                      "setup.marketplace-missing finding should be gone.")
        else:
            prompt = (f"{config} registers {len(missing)} marketplace "
                      f"folder{'s' if len(missing) != 1 else ''} that do "
                      f"not exist on this machine ({listed}). Decide per "
                      f"entry: restore the folder if it should exist here, "
                      f"repoint the entry at the real location, or remove "
                      f"it from {' and '.join(places)}, backing up each "
                      f"file first. Verify with `swe doctor`: the "
                      f"setup.marketplace-missing finding should be gone.")
        remedies.append({
            "kind": "agent",
            "title": "Fix with agent",
            "detail": "An agent decides per entry: keep, repoint, or remove.",
            "prompt": prompt,
        })
    return tuple(remedies)


# Plugin drift kinds doctor can fix by itself. ``unregistered`` and
# ``not-installed`` only add (a marketplace registration, an install from
# the user's own folder), so a batch runs them. ``stale`` removes Codex's
# copy before adding it back, so it waits for a click. ``disabled-here``
# may be a choice and ``linked-manifest`` rewrites the source, so those
# keep their command for a person to run.
_PLUGIN_ACTIONS = {"unregistered": "plugin-fix", "not-installed": "plugin-fix",
                   "stale": "plugin-reinstall"}


def plugin_findings(home: Path) -> list[DoctorFinding]:
    """``swe find plugins``'s drift, one warning per gap.

    A finding gets a Run only when its fix is commands and the harness
    CLI that runs them is on PATH; the runner recomputes drift and runs
    the commands ``plugin_drift`` builds, never text from the report.
    A drift read that fails is one warning, never a crash for doctor.
    """
    import shlex
    import shutil

    from swe.find.plugin_drift import plugin_drift
    from swe.find.roots import HARNESS_DEFAULT, harness_filter
    from swe.harness.registry import load_registry_if_present

    try:
        # An archived harness is left alone, as swe find plugins hides it:
        # a batch must never register or install into one.
        visible, _ = harness_filter(load_registry_if_present(), HARNESS_DEFAULT)
        drift = [d for d in plugin_drift(home) if visible(d.harness)]
    except Exception as exc:  # noqa: BLE001 - doctor must still report the rest
        return [DoctorFinding("plugins.unchecked", "warning",
                              f"plugin drift check could not run: {exc}")]
    findings = []
    for d in drift:
        action = _PLUGIN_ACTIONS.get(d.kind)
        if action and not (d.argv and shutil.which(d.argv[0][0])):
            action = None
        if action:
            remedy = {
                "kind": "action", "action": action, "id": d.id,
                "title": ("Reinstall from the source" if action == "plugin-reinstall"
                          else "Run it with swe"),
                "command": shlex.join(["swe", "find", "plugins", f"--fix={d.id}"]),
                "detail": d.fix,
            }
        else:
            remedy = {"kind": "command", "title": "Run it yourself",
                      "command": d.fix}
        findings.append(DoctorFinding(
            f"plugins.{d.kind}", "warning", f"{d.harness}: {d.ref}: {d.detail}",
            d.fix, harness=d.harness, action=action,
            source=d.marketplace_dir or None, remedies=(remedy,)))
    return findings


def _mcp_local_problem(cfg, strict: bool = False) -> str | None:
    """What ``swe mcp doctor`` would flag about one server, without the
    network: a stdio server's missing binary or empty env, an http one's
    bad URL scheme. Reachability stays ``swe mcp doctor``'s, since doctor
    runs on every page visit and must not wait on HEAD requests.

    ``strict`` also refuses an entry with neither command nor url, which
    ``swe mcp doctor`` only calls unknown: a hub copy must be one that
    can run before it may replace anything."""
    from swe.mcp.cli import normalize_server, probe_server_health, server_type

    if not isinstance(cfg, dict):
        return "not a server entry"
    cfg = normalize_server(cfg)
    kind = server_type(cfg)
    if kind == "stdio":
        state, detail = probe_server_health("", cfg)
        return detail if state == "error" else None
    if kind == "http":
        url = cfg.get("url", "")
        ok = isinstance(url, str) and url.lower().startswith(("http://", "https://"))
        return None if ok else "invalid URL scheme"
    if kind == "wrapped":
        args = cfg.get("args", [])
        ok = any(isinstance(a, str) and a.lower().startswith(("http://", "https://"))
                 for a in args)
        return None if ok else "no url found"
    return "no command or url" if strict else None


# A server name ``swe mcp sync --only=`` would read as a glob or a list.
_GLOB_CHARS = set("*?[],")


def mcp_findings(home: Path | None = None) -> list[DoctorFinding]:
    """``swe mcp doctor``'s problems as warnings, from local checks only.

    A broken server gets ``mcp-sync`` only when the hub (``mcp.json``)
    holds the same name, every secret it names is in the store, and its
    resolved copy passes the same checks with a command or url: then
    ``swe mcp sync swe <tool> --only=<name> --force`` replaces just
    that entry with the hub's, a click per finding since it overwrites.
    Anything else (no hub copy, a hub copy just as broken) keeps a
    command to look closer. A config the readers could not parse is one
    ``mcp.unchecked`` warning rather than silence.
    """
    import sys
    from contextlib import redirect_stdout

    # The MCP readers warn about a bad file on stdout or stderr; under
    # --json stdout is one document, and the web page sees neither.
    with redirect_stdout(sys.stderr):
        return _mcp_findings(home or Path.home())


def _mcp_findings(home: Path) -> list[DoctorFinding]:
    import io
    import shlex
    from contextlib import redirect_stderr

    from swe.console import strip_ansi

    try:
        from swe.harness.registry import load_registry_readonly
        from swe.mcp.cli import get_hub_servers, get_mcp_tools, get_tool_servers_canonical
        from swe.mcp.secrets import resolve, unresolved_names

        tools = get_mcp_tools(load_registry_readonly())
        try:
            raw_hub = get_hub_servers()
            hub = resolve(raw_hub)
        except Exception:  # noqa: BLE001 - an unreadable hub only costs the action
            raw_hub, hub = {}, {}
        findings: list[DoctorFinding] = []
        for tool, desc in sorted(tools.items()):
            if not desc["path"].exists():
                continue
            said = io.StringIO()
            with redirect_stderr(said):
                servers = get_tool_servers_canonical(tool)
            note = strip_ansi(said.getvalue()).strip()
            if note and not servers:
                findings.append(DoctorFinding(
                    "mcp.unchecked", "warning",
                    f"{tool}: couldn't read its MCP servers: {note.splitlines()[-1].strip()}",
                    source=tilde(desc["path"], home)))
                continue
            for name, cfg in sorted(servers.items()):
                problem = _mcp_local_problem(cfg)
                if problem is None:
                    continue
                fixable = (name in hub and not (_GLOB_CHARS & set(name))
                           and not unresolved_names(raw_hub.get(name), home=home)
                           and _mcp_local_problem(hub[name], strict=True) is None)
                command = shlex.join(["swe", "mcp", "sync", "swe", tool,
                                      f"--only={name}", "--force"])
                status = shlex.join(["swe", "mcp", "status", tool])
                remedies = ({"kind": "action", "action": "mcp-sync",
                             "title": "Copy the hub's entry over it",
                             "detail": f"{command}: replaces only this entry, "
                                       "with the file backed up to "
                                       "~/.swe/backups first.",
                             "command": command, "tool": tool, "server": name},
                            ) if fixable else (
                    {"kind": "command", "title": "Look closer", "command": status},)
                findings.append(DoctorFinding(
                    "mcp.server", "warning", f"{tool}: {name}: {problem}",
                    command if fixable else status,
                    action="mcp-sync" if fixable else None,
                    source=tilde(desc["path"], home),
                    remedies=remedies))
        return findings
    except Exception as exc:  # noqa: BLE001 - doctor must still report the rest
        return [DoctorFinding("mcp.unchecked", "warning",
                              f"MCP check could not run: {exc}")]


def _mcp_sync_offered(home: Path, tool: str, server: str) -> bool:
    return any(f.action == "mcp-sync" and f.remedies
               and f.remedies[0].get("tool") == tool
               and f.remedies[0].get("server") == server
               for f in mcp_findings(home))


def sync_mcp_server(home: Path, tool: str, server: str) -> str | None:
    """Replace one broken server in ``tool``'s config with the hub's copy.

    Checked again first: the finding must still be there and still
    fixable, so a stale click never overwrites a server that works now.
    The config file's contents (through a symlink, the file it points
    at) are backed up before the sync writes, and the finding is checked
    once more after: a sync that skipped the entry is an error, not a
    success. Returns what went wrong."""
    import io
    import os
    from contextlib import redirect_stdout

    from swe.mcp.cli import _sync, get_tool_config
    from swe.paths import backup_tree

    if not _mcp_sync_offered(home, tool, server):
        return f"{tool}: {server} no longer needs the hub's copy; refresh and look again"
    path = Path(os.path.realpath(get_tool_config(tool)["path"]))
    backup_tree(path, home)
    said = io.StringIO()
    with redirect_stdout(said):
        code = _sync(["swe", tool, f"--only={server}", "--force",
                      "--no-interactive"], None)
    lines = [ln for ln in said.getvalue().strip().splitlines() if ln.strip()]
    if code:
        return lines[-1] if lines else f"swe mcp sync exited {code}"
    if any(f.check == "mcp.server" and f.message.startswith(f"{tool}: {server}: ")
           for f in mcp_findings(home)):
        return (f"swe mcp sync left {tool}'s {server} as it was"
                + (f": {lines[-1]}" if lines else ""))
    return None
