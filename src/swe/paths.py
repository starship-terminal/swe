"""On-disk paths for swe user data.

Everything swe owns lives under one root, ``~/.swe``. That root holds two
kinds of thing and they are separated so the whole directory can be a git repo:

  authored     AGENTS.md, .linkignore, skills/, plugins/, hooks/,  worth versioning
               mcp/, config/
  regenerable  cache/, backups/              gitignored
  this machine data/                         gitignored
  credentials  secrets/                      gitignored, mode 700

The root sits in ``$HOME`` rather than ``$HOME/.config`` on purpose. SWE's
job is managing coding harnesses, and 24 of the 28 harness config directories
on a typical setup are ``$HOME/.<tool>``. Matching the neighbours it manages
beats matching the XDG spec here.

``~/.config/swe`` was the old root. ``legacy_config_dir`` is kept so a machine
that still has one can be migrated by ``swe init --migrate``.
"""

import contextlib
import json
import os
import re
import sys
from pathlib import Path

from swe import DATA_DIR_NAME

try:  # POSIX only; see ``locked_beside``
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

# The layout, named once. The ``*_for`` helpers take an explicit home so tests
# can build a throwaway tree; the module-level constants are the same thing
# resolved against the real home, which is what runtime code should import.
SWE_DIRNAME = f".{DATA_DIR_NAME}"

# The root's name before the project was renamed to swe. swe_dir_for moves a
# real ``~/.quiver`` across on first sight.
LEGACY_DIRNAME = ".quiver"

AGENTS_BASENAME = "AGENTS.md"
INSTRUCTIONS_SUBDIR = "AGENTS_MD"
LINKIGNORE_BASENAME = ".linkignore"
SKILLS_SUBDIR = "skills"
HOOKS_SUBDIR = "hooks"
PLUGINS_SUBDIR = "plugins"
CONFIG_SUBDIR = "config"
DATA_SUBDIR = "data"
SEARCH_SUBDIR = "search"
SEARCH_INDEX_BASENAME = "search.sqlite"
TRANSCRIPTS_SUBDIR = "transcripts"
INSIGHTS_SUBDIR = "insights"
INSIGHTS_BASENAME = "insights.sqlite"
GITHUB_SUBDIR = "github"
GITHUB_CATALOG_BASENAME = "catalog.sqlite"
REMOTE_SUBDIR = "remote"
CACHE_SUBDIR = "cache"
MCP_SUBDIR = "mcp"
MCP_SERVERS_SUBDIR = "servers"
COMPLETIONS_SUBDIR = "completions"
REPORTS_SUBDIR = "reports"
BACKUPS_SUBDIR = "backups"
SECRETS_SUBDIR = "secrets"
# One shell-style `export NAME=value` file per store, each in its own
# folder under secrets/ so the two can never be confused or merged.
SECRETS_BASENAME = ".api_keys"
PROVIDER_SECRETS_SUBDIR = "providers"
MCP_SECRETS_SUBDIR = "mcp"


_RESOLVED: dict[str, Path] = {}


def swe_dir_for(home: Path | None = None) -> Path:
    """The data root for ``home``, migrating ``~/.quiver`` to ``~/.swe`` once.

    A real ``~/.quiver`` is renamed in place (same filesystem by
    construction) and replaced by a ``~/.quiver -> .swe`` symlink, so an
    older swe running beside this one, a harness symlink pointing into the
    old root, or a config string that still names it all keep working.
    When the move can't happen — a ``~/.swe`` already exists, the rename
    fails, or ``~/.quiver`` is itself a link — the old path keeps serving
    so nothing reads an empty new root."""
    base = home or Path.home()
    root = _RESOLVED.get(str(base))
    if root is None:
        root = _resolve_data_dir(base)
        _RESOLVED[str(base)] = root
    return root


def _resolve_data_dir(base: Path) -> Path:
    new, old = base / SWE_DIRNAME, base / LEGACY_DIRNAME
    if new.exists() or new.is_symlink() or not os.path.lexists(old):
        return new
    if old.is_symlink():
        # ~/.quiver is itself a link: mirror it as ~/.swe rather than
        # move the target out from under whatever put it there. When the
        # mirror fails the old path keeps serving, so it is the answer.
        try:
            target = os.readlink(old)
            # The ~/.quiver -> .swe link an earlier move left, after
            # ~/.swe was deleted: mirroring it would make ~/.swe a link
            # to itself. Start a fresh root instead. realpath, not
            # normpath: "link/../.swe" through a linked folder is
            # somewhere else entirely.
            if os.path.realpath(os.path.join(base, target)) == os.path.realpath(new):
                return new
            new.symlink_to(target)
        except OSError:
            return old
        return new
    if not old.is_dir():
        return new
    try:
        old.rename(new)
    except OSError as exc:
        sys.stderr.write(f"swe: could not move {old} to {new} ({exc}); keeping {old}\n")
        return old
    try:
        old.symlink_to(SWE_DIRNAME)
    except OSError:
        # The move happened but the compat link did not: harness symlinks
        # pointing at ~/.quiver would dangle. Move the root back.
        try:
            new.rename(old)
            return old
        except OSError:
            pass
    try:
        changed = _rewrite_migrated_root(new, base)
    except Exception:
        changed = 0  # the move already happened; a bad rewrite never rolls it back
    try:
        _rewrite_migrated_sqlite(new, base)
    except Exception:
        pass  # a locked or surprising store keeps its old spellings; they still resolve
    note = f"swe: moved {old} to {new}; {old} is now a symlink"
    sys.stderr.write(note + (f", rewrote {changed} file(s)\n" if changed else "\n"))
    return new


# Inside a migrated root, only path-shaped and module-shaped tokens get
# rewritten: ``~/.quiver`` and absolute ``<home>/.quiver`` (via the suffix
# rule), ``python -m quiver.<mod>`` and bare ``quiver.<mod>`` argv tokens.
# ``secrets/`` is never touched, and anything the rules miss still resolves
# through the ``~/.quiver`` compatibility symlink.
_REWRITE_EXTS = {".json", ".md", ".txt", ".toml", ".yml", ".yaml", ".sh", ".py", ""}
# secrets/ must never be read, and backups are snapshots of what was.
# Everything else — including data/ stores like the ledger progress map —
# carries path strings that must be re-spelled or they go stale.
_REWRITE_SKIP_DIRS = {"backups", "secrets", ".backup"}
_REWRITE_SKIP_NAMES = {".gitignore", ".linkignore", ".gitmodules", ".DS_Store"}

_MODULE_TOKEN = re.compile(
    r"(?<![\w.])quiver\.(?=(?:bug|cli|completion_scripts|completion|config_commands|"
    r"configuration|console|find|flags|github|harness|help_text|history|hooks|"
    r"init|instructions|json_output|keys|ledger|markdown|mcp|mcp_formats|"
    r"mcp_server|multiselect|paths|plugins|prompt|providers|remote|reports|"
    r"sessions|setup|skills|status|table|webapp)\b)")
_LEGACY_TOKEN = re.compile(r"(?<!\w)\.quiver\b")


def _rewrite_migrated_root(root: Path, base: Path) -> int:
    old_abs, new_abs = str(base / LEGACY_DIRNAME), str(root)
    changed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        in_git = rel.startswith(".git") and (len(rel) == 4 or rel[4] == os.sep)
        if not in_git:
            dirnames[:] = [d for d in dirnames if d not in _REWRITE_SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if in_git:
                if name != "config":  # only worktree paths live there
                    continue
            elif name in _REWRITE_SKIP_NAMES or path.suffix.lower() not in _REWRITE_EXTS:
                continue
            if path.is_symlink():
                # A link inside the moved tree can point anywhere; writing
                # through it would edit a file the move does not own.
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out = _MODULE_TOKEN.sub("swe.", _LEGACY_TOKEN.sub(".swe", text.replace(old_abs, new_abs)))
            if out != text:
                try:
                    atomic_write_text(path, out)
                    changed += 1
                except OSError:
                    pass
    return changed


def _rewrite_migrated_sqlite(root: Path, base: Path) -> None:
    """Re-spell ``<home>/.quiver`` inside the sqlite stores under ``root``.

    Ledger events, file-tracking rows and the search index key rows by
    path strings: left spelled the old way they stop joining with the
    live tree (duplicate ledger accounts, orphaned file offsets). Every
    TEXT column gets the REPLACE, guarded by a ``LIKE old%`` so values
    that merely mention the old root without starting at it — and every
    non-path column — are untouched. ``OR IGNORE`` because a path that
    is also a key can already exist spelled the new way (the app wrote
    it between the move and this pass), and one collision must not roll
    back the rest of the file. A locked store is skipped rather than
    forced.
    """
    import sqlite3

    old_abs, new_abs = str(base / LEGACY_DIRNAME), str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _REWRITE_SKIP_DIRS and d != ".git"]
        for name in filenames:
            if not name.endswith((".sqlite", ".sqlite3", ".db")):
                continue
            path = Path(dirpath) / name
            try:
                # The app may hold the store open during the move; a short
                # busy timeout rides out a checkpoint rather than skipping.
                db = sqlite3.connect(path, timeout=5)
            except sqlite3.Error:
                continue
            try:
                tables = [r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    if table.startswith("sqlite_"):
                        continue
                    for col, ctype in ((r[1], r[2]) for r in db.execute(
                            f'PRAGMA table_info("{table}")')):
                        if "TEXT" not in ctype.upper() and "CHAR" not in ctype.upper():
                            continue
                        db.execute(
                            f'UPDATE OR IGNORE "{table}" SET "{col}" = '
                            f'REPLACE("{col}", ?, ?) '
                            f'WHERE "{col}" LIKE ?',
                            (old_abs, new_abs, f"{old_abs}%"))
                db.commit()
            except sqlite3.Error:
                pass
            finally:
                db.close()


def agents_file_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / AGENTS_BASENAME


def harness_agents_file_for(home: Path | None, name: str) -> Path:
    """A harness's own compiled instructions, ``~/.swe/AGENTS.<name>.md``."""
    return swe_dir_for(home) / f"AGENTS.{name}.md"


def instructions_dir_for(home: Path | None = None) -> Path:
    """Authored instruction chapters, compiled into the shared AGENTS.md."""
    return swe_dir_for(home) / INSTRUCTIONS_SUBDIR


def instructions_index_for(home: Path | None = None) -> Path:
    """Ordered chapter manifest for ``swe instructions build``."""
    return instructions_dir_for(home) / "index.txt"


def instructions_targets_for(home: Path | None = None) -> Path:
    """Optional per-chapter harness rules for ``swe instructions build``."""
    return instructions_dir_for(home) / "targets.json"


def linkignore_file_for(home: Path | None = None) -> Path:
    """Paths ``swe init`` must leave alone, one gitignore-style pattern per line."""
    return swe_dir_for(home) / LINKIGNORE_BASENAME


def skills_dir_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / SKILLS_SUBDIR


def hooks_dir_for(home: Path | None = None) -> Path:
    """Harness hook scripts, one subdirectory per harness registry name."""
    return swe_dir_for(home) / HOOKS_SUBDIR


def plugins_dir_for(home: Path | None = None) -> Path:
    """The user's own plugin marketplaces, one subdirectory per marketplace."""
    return swe_dir_for(home) / PLUGINS_SUBDIR


# The authored folders every hub has, in the order `swe init` reports them.
# Init creates any that are missing, so a hub that reached a second machine
# as a bare AGENTS.md ends up the same shape as one that grew in place (#696).
HUB_SUBDIRS = (SKILLS_SUBDIR, PLUGINS_SUBDIR, HOOKS_SUBDIR, MCP_SUBDIR)


def hub_dirs_for(home: Path | None = None) -> list[Path]:
    return [swe_dir_for(home) / name for name in HUB_SUBDIRS]


def config_dir_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / CONFIG_SUBDIR


def cache_dir_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / CACHE_SUBDIR


def session_parse_cache_file_for(home: Path | None = None) -> Path:
    """Per-transcript parse results the session listing reuses."""
    return cache_dir_for(home) / "session_parse_cache.json"


def session_status_cache_file_for(home: Path | None = None) -> Path:
    """What each transcript's tail says for the session status column."""
    return cache_dir_for(home) / "session_status_cache.json"


def session_model_cache_file_for(home: Path | None = None) -> Path:
    """The model each transcript's last reply ran, for the remote rows."""
    return cache_dir_for(home) / "session_model_cache.json"


def hook_events_file_for(home: Path | None = None) -> Path:
    """The hook run log: one JSON line per run, size-rotated to ``.1``.

    The hook shim writes it by hand-spelled path (``shim_runtime.events_path``
    cannot import swe); a test pins the two together.
    """
    return cache_dir_for(home) / "hooks" / "events.jsonl"


def task_states_cache_for(home: Path | None = None) -> Path:
    """The last state each stored task's source reported, which
    ``GET /v1/tasks`` serves while that source can't be read."""
    return cache_dir_for(home) / "task_states.json"


def account_homes_for(vendor: str, home: Path | None = None) -> list[Path]:
    """Each SWE account home for ``vendor``, sorted for stable order.

    SWE's extra accounts keep a harness-shaped config dir at
    ``~/.swe/accounts/<vendor>/<id>/home`` — the same layout the
    default home has, so a harness's sessions there parse exactly like
    its own. Sessions found under one belong to that account, which the
    session listing and its readers use to name the account, find its
    transcripts and resume under it.
    """
    # expanduser rather than Path.home(): tests fake harness homes by
    # patching os.path.expanduser's handling of the ``~/<dir>`` prefix,
    # which a bare ``~`` lookup would slip past and read the real dirs.
    data_dir = swe_dir_for(home or Path(os.path.expanduser("~")))
    root = data_dir / "accounts" / vendor
    try:
        return sorted(p for p in root.glob("*/home") if p.is_dir())
    except OSError:
        return []


def data_dir_for(home: Path | None = None) -> Path:
    """``~/.swe/data`` — app-owned stores (ledger, catalog, search index)."""
    return swe_dir_for(home) / DATA_SUBDIR


def search_index_for(home: Path | None = None) -> Path:
    """The FTS5 transcript-search index the app and ``swe session index`` share."""
    return data_dir_for(home) / SEARCH_SUBDIR / SEARCH_INDEX_BASENAME


def transcripts_dir_for(home: Path | None = None) -> Path:
    """``~/.swe/data/transcripts`` : the mirror of Claude and Codex
    transcripts kept past the harnesses' own retention
    (:mod:`swe.sessions.transcript_mirror`). Never delete a file in it
    unless the user asked for that session to go."""
    return data_dir_for(home) / TRANSCRIPTS_SUBDIR


def insights_db_for(home: Path | None = None) -> Path:
    """The workflow-metrics store ``swe insights`` derives from the index."""
    return data_dir_for(home) / INSIGHTS_SUBDIR / INSIGHTS_BASENAME


def issues_file_for(home: Path | None = None) -> Path:
    """The app's IssueStore, ``~/.swe/data/issues.json``.

    The app writes the actionable problems it sees (a stale sign-in, a
    denied read) here; ``swe doctor`` reads the same file so the CLI and
    the app report one set of issues.
    """
    return data_dir_for(home) / "issues.json"


def github_catalog_for(home: Path | None = None) -> Path:
    """The synced repo catalog `swe gh sync` fills and `repo search` reads."""
    return data_dir_for(home) / GITHUB_SUBDIR / GITHUB_CATALOG_BASENAME


def github_owners_cache_for(home: Path | None = None) -> Path:
    """The login-plus-orgs list a repo search and a sync are scoped to."""
    return cache_dir_for(home) / "github_owners.json"


def github_mirrors_for(home: Path | None = None) -> Path:
    """The bare clones `swe gh pr worktree` fetches PR heads into."""
    return data_dir_for(home) / GITHUB_SUBDIR / "mirrors"


def remote_dir_for(home: Path | None = None) -> Path:
    """``~/.swe/data/remote`` — the remote-control hub's state.

    Holds ``devices.json`` (paired phone credentials, mode 600),
    ``inbox_dismissals.json`` (per-device inbox dismissals, mode 600),
    ``session_reads.json`` (per-device last-read cursors, mode 600),
    ``ssh.json`` (whether paired phones' SSH keys may log in),
    ``pairing.json`` (pairing settings: owner auto-approve, off by default),
    ``machine.json`` (this machine's stable remote id), ``pair_tokens.json``
    (one-time pairing hand-offs), ``cursors/`` (per-session transcript read
    offsets), ``journal/`` (per-session event logs), ``inbox/`` (staged
    phone uploads), ``remote.sqlite`` (the session runtime's store:
    sessions, keepers, launches and command receipts), ``keepers/<slug>/``
    (one folder per session's keeper: ``keeper.json``, ``spec.json``,
    ``keeper.out`` and ``log/``, its event log), ``local.json`` (the loopback API's token and
    address, mode 600) and, on a satellite, ``agent-kept.json``. Regenerable
    except devices.json, remote.sqlite and the keepers' logs, which is why
    the whole tree stays out of the hub repo's git.
    """
    return data_dir_for(home) / REMOTE_SUBDIR


def worktrees_dir_for(home: Path | None = None) -> Path:
    """The checkouts `swe gh pr worktree` hands sessions, one per PR."""
    return swe_dir_for(home) / "worktrees"


def worktree_logs_dir_for(home: Path | None = None) -> Path:
    """Where `swe wt prune --yes` and `swe wt slim --yes` log each run."""
    return swe_dir_for(home) / "logs" / "worktree"


def mcp_dir_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / MCP_SUBDIR


def mcp_servers_dir_for(home: Path | None = None) -> Path:
    """Where the MCP server implementations themselves live.

    These are checked-out repositories and build output, ~2.7 GB across 21
    nested git repos, so the directory is gitignored even though it sits in
    the versioned half of the tree. Harness configs point at absolute paths
    inside it, which is why moving it means rewriting those configs.
    """
    return mcp_dir_for(home) / MCP_SERVERS_SUBDIR


def mcp_source_file_for(home: Path | None = None) -> Path:
    """The registry of which servers exist and where they are configured."""
    return swe_dir_for(home) / "mcp.json"


def github_file_for(home: Path | None = None) -> Path:
    """The pinned-repo store behind `swe gh` and the app's GitHub section."""
    return config_dir_for(home) / "github.json"


def config_file_for(home: Path | None = None) -> Path:
    """The small credential-free ``config.json`` ``swe config`` edits."""
    return config_dir_for(home) / "config.json"


def backups_dir_for(home: Path | None = None) -> Path:
    return swe_dir_for(home) / BACKUPS_SUBDIR


def secrets_dir_for(home: Path | None = None) -> Path:
    """Credential values, mode 700 and gitignored. Nothing else goes here."""
    return swe_dir_for(home) / SECRETS_SUBDIR


def provider_keys_file_for(home: Path | None = None) -> Path:
    """LLM provider API keys, read by `swe providers` and the app's key editor."""
    return secrets_dir_for(home) / PROVIDER_SECRETS_SUBDIR / SECRETS_BASENAME


def mcp_secrets_file_for(home: Path | None = None) -> Path:
    """The values behind the ``${NAME}`` references in mcp.json."""
    return secrets_dir_for(home) / MCP_SECRETS_SUBDIR / SECRETS_BASENAME


def legacy_provider_keys_for(home: Path | None = None) -> Path:
    """Where provider keys lived before secrets/providers/: ``~/.api_keys``.

    After `swe init` moves the file it leaves a symlink here, because shell
    profiles source this path. It may also be a directory of per-provider
    files, which `swe init` leaves in place.
    """
    return (home or Path.home()) / SECRETS_BASENAME


def legacy_mcp_secrets_file_for(home: Path | None = None) -> Path:
    """Where MCP secrets lived before secrets/mcp/."""
    return secrets_dir_for(home) / SECRETS_BASENAME


def _current_or_legacy(current: Path, legacy: Path) -> Path:
    # A machine `swe init` has not migrated yet keeps reading its old file.
    # Once the new one exists, or when neither does, the new one wins.
    if current.exists() or current.is_symlink():
        return current
    if legacy.exists():
        return legacy
    return current


def provider_keys_path_for(home: Path | None = None) -> Path:
    """The provider key store to read: the new file, else ``~/.api_keys``."""
    return _current_or_legacy(provider_keys_file_for(home), legacy_provider_keys_for(home))


def mcp_secrets_path_for(home: Path | None = None) -> Path:
    """The MCP secrets file to read: the new one, else secrets/.api_keys."""
    return _current_or_legacy(mcp_secrets_file_for(home), legacy_mcp_secrets_file_for(home))


SWE_DIR = swe_dir_for()
AGENTS_FILE = agents_file_for()
SKILLS_DIR = skills_dir_for()
BACKUPS_DIR = backups_dir_for()

CONFIG_DIR = config_dir_for()
CACHE_DIR = cache_dir_for()
MCP_DIR = mcp_dir_for()
MCP_SERVERS_DIR = mcp_servers_dir_for()
COMPLETION_DIR = SWE_DIR / COMPLETIONS_SUBDIR
REPORTS_DIR = SWE_DIR / REPORTS_SUBDIR

# Authored state: hand-edited or built up over time, worth keeping.
#
# HARNESS_FILE is the one file: what each harness is (command, description,
# aliases, tags) and what state it is in (active/starred/archived), keyed by
# name. It replaced three separate files that used to require a join by name
# to answer "show me everything about claude" — TOOLS_FILE, STARS_FILE and
# ARCHIVE_FILE below are kept defined only as the source `harness/registry.py`
# lazily migrates from on a fresh-enough install that has not been touched
# since; nothing should open them directly.
HARNESS_FILE = CONFIG_DIR / "harness.json"
TOOLS_FILE = CONFIG_DIR / "tools.json"          # legacy: migration source only
STARS_FILE = CONFIG_DIR / "stars.json"          # legacy: migration source only
ARCHIVE_FILE = CONFIG_DIR / "archived.json"     # legacy: migration source only
MCP_SOURCE_FILE = mcp_source_file_for()
SKILL_CATALOGS_FILE = CONFIG_DIR / "skill_catalogs.json"
SKILL_LINKS_FILE = CONFIG_DIR / "skill_links.json"
PROVIDERS_REGISTRY_FILE = CONFIG_DIR / "providers.json"
GITHUB_FILE = CONFIG_DIR / "github.json"
CONFIG_FILE = config_file_for()

# Regenerable state: delete any of these and swe rebuilds them.
SESSION_CACHE_FILE = CACHE_DIR / "session_cache.json"
RATE_LIMITS_CACHE_FILE = CACHE_DIR / "rate_limits_cache.json"
SESSION_COUNTS_CACHE_FILE = CACHE_DIR / "session_counts.json"
# `swe gh repo search` owner scope (login + orgs), short TTL per login.
GITHUB_OWNERS_CACHE = github_owners_cache_for()
# The synced catalog behind `swe gh sync`; only a resync rewrites it.
GITHUB_CATALOG = github_catalog_for()
# The bare mirrors and PR checkouts `swe gh pr worktree` maintains.
GITHUB_MIRRORS = github_mirrors_for()
WORKTREES_DIR = worktrees_dir_for()

# Pre-0.2.7 root. Only referenced by the migration path.
LEGACY_CONFIG_DIR = Path.home() / ".config" / "swe"

GITIGNORE_BODY = """# Regenerable. Everything else here is worth versioning.
cache/
backups/

# Run logs (`swe wt prune` and `swe wt slim`): this machine's worktree
# paths, so never versioned.
/logs/

# This machine's own stores, written by the app and swe while they run: the
# usage ledgers, the model catalog, the search index, Devin cloud sessions,
# GitHub mirrors, remote-control state, the issue list, account link records
# and their lock files. Personal, live SQLite or paths that only mean
# something here, so never versioned. Anchored: a skill or plugin may ship
# a data/ folder of its own.
/data/

# The per-PR checkouts `swe gh pr worktree` manages: each carries its own
# .git the hub should never see.
worktrees/

# Codex writes its bundled system skills into ~/.codex/skills/.system, which
# is this skills/ folder once linked. Codex owns and rewrites them.
skills/.system/

# Credential values: provider API keys in secrets/providers/ and the values
# behind mcp.json's ${NAME} references in secrets/mcp/. Keeping them out is
# what makes the rest of the hub safe to commit.
secrets/

# MCP server checkouts: ~2.7 GB of nested git repos and build output.
# mcp.json records what they are, so this can be rebuilt from a clone.
mcp/servers/
"""

# Folders a harness writes into the shared skills/ through its link, holding
# its own bundled skills. They are not hub skills: listings skip them and
# GITIGNORE_BODY keeps them out of the hub's git.
HARNESS_SKILL_DIRS = (".system",)

# Written into secrets/ itself, so the folder stays out of git even when the
# root .gitignore above is missing the line or excepts a path under it.
SECRETS_GITIGNORE_BODY = """# Credential values. Never versioned, whatever ../.gitignore says.
*
"""


def legacy_config_dir() -> Path | None:
    """The old ~/.config/swe root, if this machine still has one."""
    return LEGACY_CONFIG_DIR if LEGACY_CONFIG_DIR.is_dir() else None


def home_relative(path: "Path", home: "Path") -> str:
    """``path`` as ``~/<rest>`` under ``home``, else the absolute path.

    How every terminal view names a file. ``home`` itself reads ``~/.``.
    """
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def expand_user(value: "str | Path") -> "Path":
    """``Path(value).expanduser()`` minus the ``~user`` ``RuntimeError``.

    ``Path.expanduser`` raises ``RuntimeError`` when the named account does
    not exist, so a ``~missing-user/...`` recorded in a registry root,
    catalog entry, session record or flag value took the whole command down
    with a traceback. ``os.path.expanduser`` leaves an unresolvable
    ``~user`` literal instead — the path then probes as missing, which is
    the answer every caller was built for. ``~`` and ``~/`` still expand,
    and a ``~user`` that does exist still resolves to its home.
    """
    return Path(os.path.expanduser(str(value)))


def read_json_dict(path: "Path") -> dict:
    """The JSON object in ``path``, or ``{}``.

    For reads that can live with nothing: a missing or unreadable file,
    bytes that are not UTF-8 or not JSON, and any top-level value other
    than an object all read as ``{}``. Never build a write from its
    result: an edit of ``{}`` saved back erases whatever couldn't be
    parsed. A read-change-write needs a strict read, such as
    ``configuration.read_json_object`` or pricing's ``_write_overrides``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# path -> ((inode, mtime_ns, size), parsed value), for read_json_shared.
_SHARED_JSON: dict[tuple, tuple[tuple[int, int, int], object]] = {}


def read_json_shared(path: "Path", build=None):
    """The JSON at ``path``, parsed again only when the file changes.

    For small stores the hub reads on every request. The value is shared
    between callers: never mutate it, and never build a write from it
    (a read-change-write reads the file itself). ``build`` turns the
    parsed JSON into what is kept; its result is shared the same way.

    A change is seen through the file's inode, mtime and size: every
    writer replaces the file through ``atomic_write_text``, which gives
    it a new inode. Raises what ``open`` and ``json.loads`` raise; a
    failed read or parse is never kept.
    """
    st = os.stat(path)
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    key = (os.fspath(path), build)
    hit = _SHARED_JSON.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    with open(path, encoding="utf-8") as fh:
        data = json.loads(fh.read())
    if build is not None:
        data = build(data)
    # Keyed on the stat taken before the read: a write landing in
    # between changes the stat, so the next call reads again.
    _SHARED_JSON[key] = (sig, data)
    return data


def atomic_write_text(path: "Path", text: str, *, private: bool = False) -> None:
    """Write ``text`` to ``path`` atomically, preserving its permissions.

    The tmp-file-then-rename pattern this replaces created the temp file
    fresh under the process umask, so renaming it over a 0600 file left
    the result 0644. Harness MCP configs hold resolved API tokens and are
    0600 for that reason, and `swe mcp sync` was quietly widening them to
    world-readable on every run.

    An existing file's mode is carried over. ``private=True`` floors a new
    file at 0600, for anything that may hold credentials.
    """
    atomic_write_bytes(path, text.encode("utf-8"), private=private)


def atomic_write_bytes(path: "Path", data: bytes, *, private: bool = False) -> None:
    """``atomic_write_text`` for bytes that must land exactly as given."""
    import os
    import tempfile

    # Write through a symlink instead of replacing it: os.replace on a
    # linked config (dotfiles layout) would silently drop the link and
    # split this file from the target everyone else still reads.
    if path.is_symlink():
        path = path.resolve()

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600 if private else None

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp is 0600; widen only to whatever the file already was.
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def locked_beside(path: "Path"):
    """Hold an exclusive ``flock`` on ``<path>.lock`` for the block.

    For a read-change-write of ``path`` that another process (a second
    hub, another ``swe``) may run at the same time. The lock file is never
    replaced, so an atomic swap of ``path`` can't slip past it, and the
    lock goes when the block ends and the file is closed. The caller
    creates ``path``'s folder first. Where there is no ``fcntl``
    (Windows) the block runs unlocked.
    """
    with open(str(path) + ".lock", "a+") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def backup_tree(path: "Path", home: "Path | None" = None) -> "Path":
    """Copy a file or directory into ~/.swe/backups before it is replaced.

    Hardened against three ways the previous version failed:

    * ``relative_to(home)`` raised ValueError for anything outside $HOME,
      aborting mid-run. Paths outside home now fall back to a flattened
      absolute name.
    * ``copytree`` defaults to following symlinks, so one broken link
      anywhere in the tree raised and aborted the whole operation. Links
      are copied as links, which is also what you want when restoring.
    * Two backups of the same path in one second collided and raised
      FileExistsError. A counter disambiguates.
    """
    import shutil
    from datetime import datetime

    home = home or Path.home()
    dest_dir = backups_dir_for(home)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        flat = str(path.relative_to(home)).replace("/", "_")
    except ValueError:
        flat = str(path).lstrip("/").replace("/", "_")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{flat}.{stamp}"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{flat}.{stamp}.{n}"
        n += 1

    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, dest, symlinks=True)
    else:
        shutil.copy2(path, dest, follow_symlinks=False)
    return dest
