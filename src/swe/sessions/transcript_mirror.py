"""A local copy of Claude Code and Codex transcripts, kept past the
harnesses' own retention (#1918).

Both harnesses delete transcripts after about 30 days. This module copies
each one into ``~/.swe/data/transcripts/`` while it still exists, so the
session stays listed and readable afterwards.

Layout keeps the source's structure::

    transcripts/claude/<project-slug>/<uuid>.jsonl
    transcripts/codex/<path under sessions/ or archived_sessions/>
    transcripts/accounts/<vendor>/<account id>/<same, per SWE account home>
    transcripts/manifest.json     what was copied from where
    transcripts/mirror.json       the on/off switch and the last pass

Rules that keep user data safe, in the order they matter:

- **A mirror copy is never deleted by a pass.** Not when its source
  disappears (that is the feature), not when a copy fails, not to make
  room. The one thing a pass removes is its own half-written temp file.
  :func:`purge` is the only code that deletes a copy: one session the
  user named, whose source is gone, only the files the manifest ties to
  it, never through a symlink (``swe session mirror --purge=<id>``).
- **A restore never overwrites.** :func:`restore` puts a copy back at
  the source path the manifest recorded, only when nothing is there, and
  its last step is a hard link that fails if a file appeared meanwhile.
- **A copy is a clone where it can be.** On APFS ``clonefile(2)`` makes a
  copy-on-write clone: no bytes move, no extra disk is used while the
  source exists, and the blocks outlive the source. Anywhere else (or on
  any error) the file is streamed.
- **A copy is atomic.** Bytes go to a temp name beside the target, are
  synced, then renamed over it, so a crash leaves the old copy or the new
  one and never half a file.
- **A shrinking source cannot erase history.** When the source is now
  smaller than the copy it would replace, the old copy is kept first as a
  hard link named ``<file>.shrunk-<mtime_ns>`` (not ``.jsonl``, so nothing
  lists or indexes it).
- **A copy keeps its source's mtime**, because a session's timestamp is
  its transcript's mtime and a mirrored row must sort where it did.
- **Only the manifest says which source a copy came from.** Losing it
  loses no transcript: the next pass copies again, and the read side
  falls back to the files themselves.

Mirroring rides :func:`swe.sessions.search_writer.sources`, the one
enumeration the search indexer also uses, so a pass costs no extra
directory walk: it is handed that list. Nothing in this module reads a
transcript's bytes except the copy itself, so no value derived from here
goes into ``parse_cache``.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat as stat_mod
import sys
import uuid
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from swe.paths import (
    account_homes_for,
    atomic_write_text,
    locked_beside,
    swe_dir_for,
    transcripts_dir_for,
)

# The harnesses that prune their own transcripts. The rest keep theirs.
MIRRORED = ("claude", "codex")

MANIFEST_FILE = "manifest.json"
STATE_FILE = "mirror.json"
MANIFEST_VERSION = 1
# The manifest is rewritten after this many copies, so a pass killed
# midway has recorded most of what it did (an unrecorded copy is only
# copied again, never lost).
FLUSH_EVERY = 100
SHRUNK_SUFFIX = ".shrunk-"


class MirrorBusy(Exception):
    """Another pass holds the mirror lock and ``wait`` was false."""


# -- layout --------------------------------------------------------------


@dataclass(frozen=True)
class Root:
    """One directory a harness keeps transcripts in, and where its
    copies go."""

    harness: str
    path: str  # resolved, as ``search_writer._walk`` resolves it
    prefix: str  # subdirectory of the mirror root
    account_home: str  # "" for the harness's own home
    home: str = ""  # the harness home directory this root sits in


def roots(home: Path) -> list[Root]:
    """Every transcript root :func:`search_writer.sources` walks for the
    mirrored harnesses, with its mirror prefix."""
    out: list[Root] = []

    def add(harness: str, homedir: Path, prefix: str, account: str, subdirs) -> None:
        for sub in subdirs:
            out.append(Root(harness, os.path.realpath(homedir / sub), prefix, account,
                            str(homedir)))

    add("claude", home / ".claude", "claude", "", ("projects",))
    add("codex", home / ".codex", "codex", "", ("sessions", "archived_sessions"))
    for vendor, subdirs in (("claude", ("projects",)),
                            ("codex", ("sessions", "archived_sessions"))):
        for account_home in account_homes_for(vendor, home):
            add(vendor, account_home, f"accounts/{vendor}/{account_home.parent.name}",
                str(account_home), subdirs)
    return out


def _mirror_rel(path: str, harness: str, table: list[Root],
                origin: str | None = None) -> tuple[str, str] | None:
    """``(mirror-relative path, account_home)`` for a resolved source
    path, or None when the harness is not mirrored. A file that sits
    outside every root (a symlink pointing elsewhere) goes under a folder
    named for a hash of its path, so two of them cannot collide, inside
    the mirror root of the harness home it was found in (``origin``), so
    it keeps its account."""
    if harness not in MIRRORED:
        return None
    for root in table:
        if root.harness == harness and path.startswith(root.path + os.sep):
            return f"{root.prefix}/{path[len(root.path) + 1:]}", root.account_home
    digest = hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    name = os.path.basename(path)
    # Shaped so the listing's scans reach it: Claude only enters project
    # folders whose name starts with "-", and Codex globs `*/*/*/*.jsonl`
    # (dated) and `*.jsonl` (flat).
    prefix, account = harness, ""
    for root in table:
        if root.harness == harness and origin is not None and root.home == origin:
            prefix, account = root.prefix, root.account_home
            break
    if harness == "claude":
        return f"{prefix}/-external-{digest}/{name}", account
    return f"{prefix}/external/{digest}/0/{name}", account


# -- settings ------------------------------------------------------------


def _state_path(home: Path | None) -> Path:
    return transcripts_dir_for(home) / STATE_FILE


def load_state(home: Path | None = None) -> dict:
    """``{"enabled": bool, "last_pass": {...}}``. Mirroring is on until
    someone says ``--off``: it is a copy on local disk, and a machine
    that never opts in should still keep what it would lose."""
    try:
        data = json.loads(_state_path(home).read_text())
    except (OSError, ValueError):
        return {"enabled": True}
    return data if isinstance(data, dict) else {"enabled": True}


def is_enabled(home: Path | None = None) -> bool:
    return load_state(home).get("enabled") is not False


def _update_state(home: Path | None, change) -> dict:
    path = _state_path(home)
    _ensure_dir(path.parent, transcripts_dir_for(home))
    with locked_beside(path):
        data = load_state(home)
        change(data)
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", private=True)
        return data


def set_enabled(home: Path | None, enabled: bool) -> None:
    _update_state(home, lambda data: data.update(enabled=bool(enabled)))


# -- manifest ------------------------------------------------------------


def manifest_path(home: Path | None = None) -> Path:
    return transcripts_dir_for(home) / MANIFEST_FILE


_manifest_cache: dict[str, tuple[tuple, dict]] = {}


def _manifest_signature(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


# Entry fields that must be numbers for a reader to use them. A value of
# another type reads as 0 (the stat then never matches, so a live source
# is copied again, which is what an unknown stat should mean).
_NUMERIC_FIELDS = ("ino", "mtime_ns", "size", "mirror_size", "copied_at")


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def _clean_entry(value) -> dict | None:
    """The entry as readers may use it, or None when it cannot be read: not
    an object, no ``source`` path string, no ``harness`` string, or an
    ``account_home`` that is not a string. Wrong-typed numbers read as 0."""
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("source"), str) or not value["source"]:
        return None
    if not isinstance(value.get("harness"), str):
        return None
    account = value.get("account_home")
    if account is not None and not isinstance(account, str):
        return None
    clean = dict(value)
    clean["account_home"] = account or ""
    for field in _NUMERIC_FIELDS:
        if field in clean and not _is_number(clean[field]):
            clean[field] = 0
    return clean


def _read_manifest_full(path: Path) -> tuple[dict, dict, bool]:
    """``(raw, entries, corrupt)``: ``raw`` is the file's ``files`` object
    exactly as stored, ``entries`` only the well-formed ones, cleaned
    (:func:`_clean_entry`). A missing file is empty and not corrupt."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, {}, False
    except (OSError, ValueError, RecursionError):
        return {}, {}, True
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}, {}, True
    entries = {}
    for key, value in files.items():
        clean = _clean_entry(value)
        if clean is not None:
            entries[key] = clean
    return files, entries, False


def _read_manifest(path: Path) -> tuple[dict, bool]:
    """``(entries, corrupt)``: only the well-formed entries, so no reader
    ever sees a malformed one. A missing file is empty and not corrupt."""
    _raw, entries, corrupt = _read_manifest_full(path)
    return entries, corrupt


def load_manifest(home: Path | None = None) -> dict[str, dict]:
    """``{mirror-relative path: entry}``; empty when there is none or it
    cannot be read. Never raises, and is cheap to call again: the parse is
    kept until the file's stat changes."""
    path = manifest_path(home)
    signature = _manifest_signature(path)
    if signature is None:
        return {}
    cached = _manifest_cache.get(str(path))
    if cached is not None and cached[0] == signature:
        return cached[1]
    entries, _ = _read_manifest(path)
    _manifest_cache[str(path)] = (signature, entries)
    return entries


def _write_manifest(home: Path | None, entries: dict, raw: dict | None = None,
                    before: dict | None = None) -> None:
    """Write ``entries``. Given the ``raw`` file and the ``before`` entries
    it was read as, only what the caller changed is written: an entry it
    added or changed takes its new value, one it removed goes, and every
    other key keeps its stored value verbatim, a malformed one included,
    so no writer ever drops an entry it could not read."""
    if raw is not None:
        before = before or {}
        merged = dict(raw)
        for key in before:
            if key not in entries:
                merged.pop(key, None)
        for key, value in entries.items():
            if before.get(key) != value:
                merged[key] = value
        entries = merged
    body = {"version": MANIFEST_VERSION, "files": entries}
    atomic_write_text(manifest_path(home), json.dumps(body, indent=1, sort_keys=True) + "\n",
                      private=True)


def _ensure_dir(path: Path, stop: Path) -> None:
    """``mkdir -p`` with 0700 on every directory from ``stop`` down:
    ``mkdir(parents=True, mode=...)`` applies the mode to the leaf only."""
    missing = []
    current = path
    while not current.exists() and current != stop and current != current.parent:
        missing.append(current)
        current = current.parent
    if not current.exists():
        current.mkdir(parents=True, mode=0o700, exist_ok=True)
    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)


# -- one pass ------------------------------------------------------------


@dataclass
class MirrorResult:
    files: int = 0  # mirrored-harness transcripts the enumeration held
    copied: int = 0  # streamed copies
    cloned: int = 0  # copy-on-write clones (macOS/APFS): no bytes moved
    unchanged: int = 0
    orphaned: int = 0  # copies whose source is gone: kept, as intended
    failed: int = 0
    conflicts: int = 0  # a copy owned by another source: left alone
    unreadable: int = 0  # a copy whose manifest entry cannot be read: left alone
    copied_bytes: int = 0
    cloned_bytes: int = 0
    unchanged_bytes: int = 0
    orphaned_bytes: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "files", "copied", "cloned", "unchanged", "orphaned", "failed", "conflicts", "unreadable",
            "copied_bytes", "cloned_bytes", "unchanged_bytes", "orphaned_bytes", "seconds")}


def _unchanged(entry: dict | None, path: str, st: os.stat_result, dest: Path) -> bool:
    if not entry or entry.get("source") != path:
        return False
    if (entry.get("size"), entry.get("mtime_ns"), entry.get("ino")) != (
            st.st_size, st.st_mtime_ns, st.st_ino):
        return False
    try:
        return os.stat(dest).st_size == entry.get("mirror_size")
    except OSError:
        return False  # the copy is gone: copy again


def _keep_shrunk(dest: Path, written: int) -> None:
    """Before ``dest`` is replaced by something smaller, keep it under a
    second name. A hard link, so no bytes move and no gap opens."""
    try:
        old = os.stat(dest)
    except FileNotFoundError:
        return
    if old.st_size <= written:
        return
    backup = dest.with_name(f"{dest.name}{SHRUNK_SUFFIX}{old.st_mtime_ns}")
    try:
        os.link(dest, backup)
    except FileExistsError:
        pass  # this exact version is already kept


def _clone(source: str, target: str) -> None:
    """``clonefile(2)``: a copy-on-write clone on APFS. No bytes move and
    no extra disk is used while the source exists; the blocks stay alive
    after the harness deletes the source. ``target`` must not exist.
    Raises ``OSError`` for anything that stops it (not macOS, a missing
    symbol, another volume, a filesystem without clones)."""
    import ctypes

    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "clonefile is macOS only")
    try:
        fn = ctypes.CDLL(None, use_errno=True).clonefile
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOTSUP, "clonefile is unavailable") from exc
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(source), os.fsencode(target), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), source)


def _copy(source: str, dest: Path, root: Path, *,
          exclusive: bool = False) -> tuple[os.stat_result, int, str]:
    """Put a copy of ``source`` at ``dest`` atomically: a clone where the
    filesystem can, else a streamed copy. Returns ``(stat, bytes, how)``
    with ``how`` ``"cloned"`` or ``"copied"``.

    ``exclusive`` is the restore direction: ``dest`` must not exist and
    is never replaced. Its folder must resolve to itself (no symlink on
    the way, since the recorded source path is already resolved), and the
    last step is a hard link, which fails with ``FileExistsError`` if a
    file appeared at ``dest`` meanwhile. The stat returned is then the
    published file's, taken before it was published.

    The stat is what the manifest records, and it never claims more than
    the copy holds. Cloning: the source is stat'ed *before* the clone, so
    a source that grew in between yields a clone with more than the
    recorded size, and the next pass sees a changed stat and clones
    again. Streaming: the stat comes from the open handle before the
    first byte is read, for the same reason. ``bytes`` is the copy's own
    size (``mirror_size``), never the source's."""
    if exclusive:
        # Check before creating anything, so no folder is made through a
        # link, and again after, in case one was swapped in meanwhile.
        existing = dest.parent
        while not os.path.lexists(existing) and existing != existing.parent:
            existing = existing.parent
        if not _resolves_to_itself(str(existing)):
            raise OSError(errno.ELOOP, "a folder on the way is a symlink", str(existing))
    elif not _resolves_to_itself(str(root)) or not _under(str(dest), str(root)):
        raise OSError(errno.EINVAL, "a mirror copy goes under the mirror's real root", str(dest))
    else:
        # Forward: no folder between the root and the copy may be a link,
        # or the copy could replace a file in another account's folder.
        existing = dest.parent
        while not os.path.lexists(existing) and existing != root:
            existing = existing.parent
        if not _resolves_to_itself(str(existing)):
            raise OSError(errno.ELOOP, "a mirror folder is a symlink", str(existing))
    _ensure_dir(dest.parent, root)
    if not _resolves_to_itself(str(dest.parent)):
        raise OSError(errno.ELOOP, "a folder on the way is a symlink", str(dest.parent))
    # A name nothing has created: clonefile refuses an existing target.
    tmp = str(dest.parent / f".{dest.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        try:
            st = os.stat(source)
            _clone(source, tmp)
            how = "cloned"
            written = os.stat(tmp).st_size
        except OSError:
            # No clone here: stream it, exactly as before.
            with contextlib.suppress(OSError):
                os.unlink(tmp)  # our own temp name, if a clone half-made it
            how = "copied"
            with open(source, "rb") as src, open(tmp, "xb") as out:
                st = os.fstat(src.fileno())
                written = 0
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                out.flush()
                os.fsync(out.fileno())
        # A clone carries the source's times already; this pins both
        # paths to the stat the manifest records.
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.chmod(tmp, 0o600)
        if exclusive:
            # The stat of exactly the bytes written, before anything else
            # can reach them: the file published next is this inode, so a
            # harness append after it reads as a change on the next pass.
            published = os.stat(tmp)
            _link_new(tmp, dest)
            os.unlink(tmp)
            return published, written, how
        _keep_shrunk(dest, written)
        os.replace(tmp, dest)
        return st, written, how
    except BaseException:
        # Our own unfinished file, never a transcript.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _link_new(tmp: str, dest: Path) -> None:
    """Give ``tmp`` the name ``dest`` without ever replacing a file
    there: a hard link fails with ``FileExistsError`` when ``dest``
    exists. A filesystem that refuses hard links fails the restore
    rather than fall back to a rename, which would replace."""
    os.link(tmp, dest, follow_symlinks=False)


def sync(home: Path, sources: Iterable, should_stop=None, *, wait: bool = True) -> MirrorResult:
    """Copy every mirrored-harness file in ``sources`` that is new or
    changed, and count the copies whose source is gone.

    ``sources`` is what :func:`search_writer.sources` returned (anything
    with ``path`` and ``harness``); the caller owns the enumeration.
    Nothing is deleted. Raises :class:`MirrorBusy` when ``wait`` is false
    and another pass is running."""
    started = time.monotonic()
    result = MirrorResult()
    root = transcripts_dir_for(home)
    table = roots(home)
    _ensure_dir(root, root.parent)
    # Copies are written under the root's real path, so the check in
    # ``_copy`` can tell a linked folder inside the mirror (which could
    # land a copy on another account's) from the root's own spelling.
    root = Path(os.path.realpath(root))
    with _pass_lock(home, wait):
        raw, entries, corrupt = _read_manifest_full(manifest_path(home))
        before = dict(entries)
        unreadable = set(raw) - set(entries)
        if corrupt:
            # Never overwrite what cannot be read: set it aside, and the
            # copies below rebuild the record.
            with contextlib.suppress(OSError):
                os.replace(manifest_path(home),
                           manifest_path(home).with_name(f"{MANIFEST_FILE}.corrupt-{int(time.time())}"))
        seen: set[str] = set()
        dirty = False
        pending = 0
        try:
            for source in sources:
                if should_stop is not None and should_stop():
                    break
                located = _mirror_rel(source.path, source.harness, table,
                                      getattr(source, "home", None))
                if located is None:
                    continue
                rel, account_home = located
                result.files += 1
                seen.add(rel)
                dest = root / rel
                try:
                    st = os.stat(source.path)
                except OSError:
                    continue  # gone between the walk and now: orphan next pass
                if rel in unreadable:
                    # Its record cannot be read, so whether this source
                    # owns the copy is unknown: leave the copy alone.
                    result.unreadable += 1
                    continue
                entry = entries.get(rel)
                if (entry is not None and entry.get("source") != source.path
                        and not _is_gone(entry)):
                    # Two live sources claim one mirror path. Whichever
                    # got there first keeps it: overwriting could lose
                    # the only copy of the other. (A recorded source that
                    # is gone is a moved harness home: the same session
                    # at a new place, so this one takes over.)
                    result.conflicts += 1
                    continue
                if _unchanged(entry, source.path, st, dest):
                    result.unchanged += 1
                    result.unchanged_bytes += entry.get("mirror_size", 0)
                    continue
                try:
                    taken, written, how = _copy(source.path, dest, root)
                except OSError:
                    result.failed += 1
                    continue
                entries[rel] = {
                    "harness": source.harness,
                    "source": source.path,
                    "account_home": account_home,
                    "ino": taken.st_ino,
                    "mtime_ns": taken.st_mtime_ns,
                    "size": taken.st_size,
                    "mirror_size": written,
                    "copied_at": round(time.time(), 3),
                }
                if how == "cloned":
                    result.cloned += 1
                    result.cloned_bytes += written
                else:
                    result.copied += 1
                    result.copied_bytes += written
                dirty = True
                pending += 1
                if pending >= FLUSH_EVERY:
                    _write_manifest(home, entries, raw, before)
                    pending = 0
            # Copies whose source is no longer anywhere: counted, kept.
            for rel, entry in entries.items():
                if rel in seen or os.path.lexists(entry.get("source", "")):
                    continue
                if (root / rel).is_file():
                    result.orphaned += 1
                    result.orphaned_bytes += entry.get("mirror_size", 0)
        finally:
            if dirty or corrupt:
                _write_manifest(home, entries, None if corrupt else raw, before)
    result.seconds = round(time.monotonic() - started, 3)
    stamp = {**result.as_dict(), "finished_at": time.time()}
    with contextlib.suppress(OSError):
        _update_state(home, lambda data: data.update(last_pass=stamp))
    return result


@contextlib.contextmanager
def _pass_lock(home: Path, wait: bool):
    """One pass at a time per machine: the hub's loop, ``swe session
    index`` and ``swe session mirror`` can all run it."""
    path = manifest_path(home)
    if wait:
        with locked_beside(path):
            yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: locked_beside is a no-op too
        yield
        return
    with open(str(path) + ".lock", "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MirrorBusy("another transcript mirror pass is running") from None
        yield


def run_pass(home: Path | None = None, should_stop=None, *, wait: bool = True) -> MirrorResult:
    """One pass on its own: enumerate, then :func:`sync`. For the callers
    that have no index pass to ride (the hub with search indexing off,
    ``swe session mirror``)."""
    from swe.sessions import search_writer  # function-local: it imports this module

    home = Path(home) if home is not None else Path.home()
    return sync(home, search_writer.sources(home), should_stop, wait=wait)


# -- restore, purge and upkeep -------------------------------------------
#
# The only code here that writes outside the mirror root is restore, and
# only at the source path the manifest recorded, only when nothing is
# there. The only code that deletes a copy is purge.


class Refused(Exception):
    """A restore, purge or sweep that would not be safe; the message says
    why, in words a user can act on."""


# The temp names this module writes: a copy's (``_copy``) beside its
# target, and ``atomic_write_text``'s for the two state files in the root.
_COPY_TMP = re.compile(r"^\..+\.\d+-[0-9a-f]{32}\.tmp$")
# A restore's temp beside its target: the same naming, the name captured.
_RESTORE_TMP = re.compile(r"^\.(.+)\.\d+-[0-9a-f]{32}\.tmp$")
_STATE_TMP = re.compile(r"^\.(?:manifest|mirror)\.json\.[A-Za-z0-9_]+\.tmp$")
STALE_TMP_S = 24 * 3600


def _under(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip(os.sep) + os.sep)


def _real_root(home: Path | None) -> str:
    return os.path.realpath(transcripts_dir_for(home))


def _resolves_to_itself(path: str) -> bool:
    """No ``..``, and no symlink anywhere on the way to ``path``."""
    norm = os.path.normpath(path)
    return path == norm and os.path.realpath(norm) == norm


def _inside(path: str, root: str) -> bool:
    """``path`` is a regular file (not a symlink) inside ``root``, spelled
    without ``..``, and no folder between them is a symlink."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat_mod.S_ISREG(st.st_mode) and _under(path, root)
            and _resolves_to_itself(path))


def _group_rel(harness: str, account_home: str) -> str:
    """The mirror folder, relative to the root, that holds ``harness``
    copies for an account home (``""`` for the harness's own)."""
    if account_home:
        return f"accounts/{harness}/{Path(account_home).parent.name}"
    return harness


def _group_dir(home: Path | None, harness: str, account_home: str) -> str | None:
    """The real path of a session's account folder in the mirror, spelled
    from the real root and checked before anything is resolved through
    it: None when there is none, :class:`Refused` when it is a link (or
    sits behind one), which could lead into another account's copies."""
    if harness not in MIRRORED:
        return None
    path = os.path.join(_real_root(home), _group_rel(harness, account_home))
    if not os.path.lexists(path):
        return None
    if not _resolves_to_itself(path) or not os.path.isdir(path):
        raise Refused(f"{path} is a link or not a folder; swe will not follow it, since it "
                      "could lead to another account's copies")
    return path


def _same_account(entry: dict, account_home: str) -> bool:
    return os.path.normpath(entry.get("account_home") or "") == \
        os.path.normpath(account_home or "")


def _session_rels(entries: dict, session, home: Path | None) -> tuple[list[str], list[str]]:
    """``(main, extra)`` mirror-relative paths the manifest ties to
    ``session``: its own transcript copies (several for a Claude session
    resumed from another folder, every rollout for a Codex thread) and
    the subagent transcripts in the folder Claude keeps beside one. Only
    entries recorded for the session's own account home count, inside
    its account folder, which must not be a link (:func:`_group_dir`)."""
    group = _group_dir(home, session.tool_name, session.account_home)
    if group is None:
        return [], []
    root = _real_root(home)
    stem = Path(session.session_id).stem
    thread = ""
    if session.tool_name == "codex":
        from swe.sessions.parsers import codex_thread_id  # function-local: parsers imports this module

        thread = codex_thread_id(stem)
    main: list[str] = []
    for rel, entry in entries.items():
        if entry.get("harness") != session.tool_name \
                or not _same_account(entry, session.account_home):
            continue
        path = os.path.join(root, rel)
        if os.path.normpath(path) != path or not _under(path, group):
            continue  # a key spelled with ``..``, or another account's
        name = os.path.basename(rel)
        if not name.endswith(".jsonl"):
            continue
        if thread:
            from swe.sessions.parsers import codex_thread_id

            same = codex_thread_id(name[:-len(".jsonl")]) == thread
        else:
            same = name == f"{stem}.jsonl"
        if same:
            main.append(rel)
    extra: list[str] = []
    if session.tool_name == "claude":
        folders = tuple(f"{rel[:-len('.jsonl')]}/" for rel in main)
        extra = [rel for rel, entry in entries.items()
                 if rel not in main and entry.get("harness") == "claude"
                 and _same_account(entry, session.account_home)
                 and rel.startswith(folders)] if folders else []
    return sorted(main), sorted(extra)


def _thread_of_name(name: str) -> str:
    from swe.sessions.parsers import codex_thread_id  # function-local: parsers imports this module

    return codex_thread_id(name[:-len(".jsonl")] if name.endswith(".jsonl") else name)


def _thread_of(session) -> str:
    """A Codex session's thread uuid, or "" for anything else."""
    if session.tool_name != "codex":
        return ""
    return _thread_of_name(Path(session.session_id).stem)


def restore(home: Path | None, session) -> dict:
    """Put the mirror copy a read of ``session`` would use (the one
    :func:`find_copy` picks, which the listing shows) back at the source
    path its manifest entry recorded, so the harness can resume it.

    Refuses (:class:`Refused`) when anything is at that path, when the
    manifest does not say where the copy came from, when the copy is not
    a plain file inside the mirror, and for a source outside the harness
    roots unless its folder is still there and a link in the harness
    root still points at it (otherwise the restored file would not
    list). Never overwrites: see ``_copy(exclusive=True)``. Raises
    :class:`MirrorBusy` while a pass runs."""
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    root = _real_root(home)
    copy_path = _listed_copy(home, session)
    rel = copy_path[len(root) + 1:]
    with _pass_lock(home, wait=False):
        raw, entries, _corrupt = _read_manifest_full(manifest_path(home))
        before = dict(entries)
        if rel in raw and rel not in entries:
            raise Refused(f"the manifest entry for {rel} cannot be read, so swe cannot tell "
                          "where to put it back")
        entry = entries.get(rel)
        if entry is None:
            raise Refused(f"the copy the listing shows, {copy_path}, has no manifest record of "
                          "where it was copied from, so swe cannot tell where to put it back "
                          "(and will not restore an older copy in its place)")
        if entry.get("harness") != session.tool_name \
                or not _same_account(entry, session.account_home):
            raise Refused(f"the manifest records {rel} for another harness or account home, "
                          "so swe will not restore it for this session")
        target = entry.get("source")
        if not isinstance(target, str) or not os.path.isabs(target) \
                or os.path.normpath(target) != target:
            raise Refused(f"the manifest's source path for {rel} is not a usable path")
        if os.path.lexists(target):
            raise Refused(f"{target} already exists; swe never overwrites a transcript")
        stop = _restore_stop(home, session.tool_name, target)
        try:
            taken, written, how = _copy(copy_path, Path(target), stop, exclusive=True)
        except FileExistsError:
            raise Refused(f"{target} appeared while restoring; swe left it alone") from None
        except OSError as exc:
            raise Refused(f"could not restore to {target}: {exc.strerror or exc}") from None
        # The restored file is the source now. Its stat was taken before
        # it was published, so it describes exactly the bytes the copy
        # holds: the next pass finds the copy current, and anything the
        # harness appends since reads as a change and is copied.
        entries[rel] = {**entry, "ino": taken.st_ino, "mtime_ns": taken.st_mtime_ns,
                        "size": taken.st_size}
        with contextlib.suppress(OSError):
            _write_manifest(home, entries, raw, before)
    return {"harness": session.tool_name, "session_id": session.session_id,
            "account_home": session.account_home or None, "copy": copy_path,
            "target": target, "bytes": written, "how": how}


def _listed_copy(home: Path, session) -> str:
    """The copy the listing shows for ``session``: among the plain files
    named ``<session id>.jsonl`` in its account folder, tracked by the
    manifest or not, the newest by mtime (the listing's own rule). Found
    by walking the checked folder without following links, so a newer
    copy a crash left untracked is the one chosen, and restore then
    refuses it rather than bring back older history."""
    group = _group_dir(home, session.tool_name, session.account_home)
    if group is None or not session.session_id:
        raise Refused("swe has no mirror copy of this session")
    root = _real_root(home)
    name = f"{Path(session.session_id).stem}.jsonl"
    found = []
    for folder, _dirs, files in os.walk(group):
        if name in files:
            path = os.path.join(folder, name)
            if _inside(path, root):
                found.append(path)
    if not found:
        raise Refused("swe has no mirror copy of this session (a copy that is a symlink, "
                      "or behind one, is never used)")
    return max(found, key=lambda p: (os.lstat(p).st_mtime_ns, p))


def _restore_stop(home: Path, harness: str, target: str) -> Path:
    """The folder restore may create ``target``'s parents down from, or
    :class:`Refused` when it may not write there at all."""
    table = [root for root in roots(home) if root.harness == harness]
    for root in table:
        if _under(target, root.path):
            if not os.path.isdir(root.home):
                raise Refused(f"{root.home} does not exist, so {harness} is not set up "
                              "there any more")
            return Path(root.home)
    # A source outside every harness root was reached through a link.
    parent = os.path.dirname(target)
    stop = None
    if os.path.isdir(parent):
        stop = Path(parent)
    else:
        for root in table:
            home_dir = os.path.realpath(root.home)
            if os.path.isdir(home_dir) and _under(target, home_dir):
                stop = Path(home_dir)
                break
    if stop is None:
        raise Refused(f"{parent} is gone and is outside {harness}'s home; "
                      "swe will not recreate it")
    if not _linked_from(table, target):
        raise Refused(f"no link in {harness}'s session folders points at {target} any more, "
                      "so a restored file would not list; swe left it alone")
    return stop


def _linked_from(table: list[Root], target: str) -> bool:
    """Whether a symlink inside one of ``table``'s roots resolves to
    ``target``. Only the roots are walked, without following links."""
    for root in table:
        for folder, dirs, files in os.walk(root.path):
            for name in files + dirs:
                path = os.path.join(folder, name)
                if os.path.islink(path) and os.path.realpath(path) == target:
                    return True
    return False


def purge_plan(home: Path | None, session) -> dict:
    """What :func:`purge` would remove for ``session``, without removing
    anything: ``{harness, session_id, account_home, entries, files,
    bytes, untracked}``.

    ``entries`` are the manifest entries tied to the session, ``files``
    the plain files inside the mirror root they name, plus each one's
    ``.shrunk-*`` backups. ``untracked`` are copies of the session the
    manifest does not know: they are named, never removed. Refuses
    (:class:`Refused`) while any of the session's own transcripts still
    exists at its source, and when any file it would remove is a symlink
    or sits behind one."""
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    entries = load_manifest(home)
    main, extra = _session_rels(entries, session, home)
    if not main:
        raise Refused("the manifest ties no mirror copy to this session")
    live = [entries[rel]["source"] for rel in main if not _is_gone(entries[rel])]
    if live:
        raise Refused(f"its transcript is still at {live[0]}; a purge only removes the copy "
                      "of a session its harness has deleted")
    # A subagent transcript still on disk is copied again by the next
    # pass anyway: leave its entry and copy.
    rels = main + [rel for rel in extra if _is_gone(entries[rel])]
    root = _real_root(home)
    files: list[str] = []
    skipped: list[str] = []
    for rel in rels:
        path = os.path.join(root, rel)
        if os.path.lexists(path):
            if not _inside(path, root):
                skipped.append(path)
                continue
            files.append(path)
        # The backups are scanned even when the copy itself is gone (a
        # purge interrupted between the two), or they would outlive
        # their entry and no later purge could name them.
        folder, name = os.path.split(path)
        if not os.path.lexists(folder):
            continue
        if not _resolves_to_itself(folder):
            skipped.append(folder)
            continue
        with contextlib.suppress(OSError):
            for child in sorted(os.listdir(folder)):
                if child.startswith(name + SHRUNK_SUFFIX) and child[len(name + SHRUNK_SUFFIX):].isdigit():
                    backup = os.path.join(folder, child)
                    (files if _inside(backup, root) else skipped).append(backup)
    known = {os.path.join(root, rel) for rel in entries}
    untracked = []
    group = _group_dir(home, session.tool_name, session.account_home)
    if group is not None:
        names = {os.path.basename(rel) for rel in main}
        thread = _thread_of(session)
        for folder, _dirs, names_here in os.walk(group):
            for name in names_here:
                path = os.path.join(folder, name)
                if path in known or not name.endswith(".jsonl"):
                    continue
                # Codex names every rollout of a thread differently: a
                # rollout the manifest never recorded is the thread's too.
                if name in names or (thread and _thread_of_name(name) == thread):
                    untracked.append(path)
    if skipped:
        raise Refused(f"{skipped[0]} is a symlink or not a plain file inside the mirror; "
                      "swe never follows one out of the mirror, so nothing was removed")
    size = 0
    for path in files:
        with contextlib.suppress(OSError):
            size += os.lstat(path).st_size
    return {"harness": session.tool_name, "session_id": session.session_id,
            "account_home": session.account_home or None, "entries": rels,
            "files": files, "bytes": size, "untracked": sorted(untracked)}


def purge(home: Path | None, plan: dict) -> dict:
    """Remove what ``plan`` (from :func:`purge_plan`) listed, and drop its
    manifest entries. The plan is worked out again under the pass lock and
    must name the same files and entries, so nothing is removed that was
    not shown. The copies go first, then the entries: a crash in between
    leaves entries with no copy (``swe doctor`` reports them), never a
    copy that lists again without its record. Raises :class:`MirrorBusy`
    while a pass runs."""
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    from swe.sessions.models import Session

    session = Session(timestamp=0, agent="", path="", session_id=plan["session_id"],
                      tool_name=plan["harness"], account_home=plan.get("account_home") or "")
    root = _real_root(home)
    with _pass_lock(home, wait=False):
        _manifest_cache.pop(str(manifest_path(home)), None)
        again = purge_plan(home, session)
        if (again["files"], again["entries"]) != (plan["files"], plan["entries"]):
            raise Refused("the mirror changed since the list was shown; nothing was removed, "
                          "run it again")
        removed: list[str] = []
        for path in again["files"]:
            if not _inside(path, root):
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            removed.append(path)
        raw, entries, corrupt = _read_manifest_full(manifest_path(home))
        purged = [entries[rel] for rel in again["entries"] if rel in entries]
        if not corrupt:
            before = dict(entries)
            for rel in again["entries"]:
                entries.pop(rel, None)
            _write_manifest(home, entries, raw, before)
    return {**again, "removed": removed, "search": _forget_searched(home, again, purged)}


def _search_identity(harness: str, name: str) -> str:
    """The id the search index files a transcript under: the Codex thread
    uuid every rollout of a thread carries, else the file's stem (the
    Claude session id, or a name with no session uuid such as ``notes``)."""
    stem = Path(name).stem
    if harness == "codex":
        return _thread_of_name(stem) or stem
    return stem


def _forget_searched(home: Path, plan: dict, purged: list[dict]) -> str:
    """Drop the purged session from the search index once its copies are
    gone (copies first, or a pass in between reads them again): one
    ``forget_session(home, harness, id, source_path=source)`` per purged
    manifest entry, so a transcript whose name holds no session uuid is
    found by its source path. An older three-argument ``forget_session``
    is called once per id instead, and a search writer without one (it
    indexes no copies) has nothing to forget. Returns ``"forgotten"``,
    ``"none"`` or the reason it could not; a purge never fails over it."""
    from swe.sessions import search_writer  # function-local: it imports this module

    forget = getattr(search_writer, "forget_session", None)
    if forget is None:
        return "none"
    calls = [(entry["harness"], _search_identity(entry["harness"], entry["source"]),
              entry["source"]) for entry in purged]
    if not calls:
        calls = [(plan["harness"], _search_identity(plan["harness"], plan["session_id"]), None)]
    try:
        try:
            for harness, identity, source in calls:
                forget(home, harness, identity, source_path=source)
        except TypeError:
            # A forget_session that takes no source_path: once per id.
            for harness, identity in dict.fromkeys((h, i) for h, i, _s in calls):
                forget(home, harness, identity)
    except Exception as exc:  # IndexBusy, IndexUnavailable, a broken index
        return f"not removed from search: {exc}"
    return "forgotten"


def _age(st: os.stat_result, now: float) -> float:
    """How long ago a temp file was last touched. ``_copy`` sets a temp's
    mtime to its source's before the rename, so the mtime alone can be
    years old for a file written a moment ago: the newer of mtime and
    ctime is the age."""
    return now - max(st.st_mtime, st.st_ctime)


def _stale(path: str, now: float, older_than: float) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat_mod.S_ISREG(st.st_mode) and _age(st, now) >= older_than


def _restore_leftovers(home: Path | None, now: float, older_than: float) -> list[str]:
    """A restore's own temp files a crash left: in each folder the
    manifest records a source in (that folder only, never a wider scan),
    named exactly as ``_copy`` names the temp for that source
    (``.<source name>.<pid>-<32 hex>.tmp``). A folder that does not
    resolve to itself is skipped."""
    by_folder: dict[str, set[str]] = {}
    for entry in load_manifest(home).values():
        source = entry.get("source")
        if not isinstance(source, str) or not os.path.isabs(source) \
                or os.path.normpath(source) != source:
            continue
        folder, name = os.path.split(source)
        by_folder.setdefault(folder, set()).add(name)
    found: list[str] = []
    for folder, names in by_folder.items():
        if not os.path.isdir(folder) or not _resolves_to_itself(folder):
            continue
        with contextlib.suppress(OSError):
            for child in os.listdir(folder):
                match = _RESTORE_TMP.match(child)
                if match is None or match.group(1) not in names:
                    continue
                path = os.path.join(folder, child)
                if _stale(path, now, older_than):
                    found.append(path)
    return found


def temp_files(home: Path | None = None, older_than: float = STALE_TMP_S) -> list[str]:
    """This module's own temp files that nothing has touched for
    ``older_than`` seconds: what a hard crash mid-copy leaves under the
    mirror root, and what one mid-restore leaves beside a recorded source
    (:func:`_restore_leftovers`). Plain files only; symlinked folders are
    not entered."""
    root = _real_root(home)
    if not os.path.isdir(root):
        return []
    now = time.time()
    found: list[str] = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if not (_COPY_TMP.match(name) or (folder == root and _STATE_TMP.match(name))):
                continue
            path = os.path.join(folder, name)
            if _stale(path, now, older_than):
                found.append(path)
    found += _restore_leftovers(home, now, older_than)
    return sorted(found)


def sweep_temps(home: Path | None = None) -> list[str]:
    """Remove :func:`temp_files` under the pass lock, so no copy or
    restore in flight can lose its temp (a restore holds the same lock).
    Each path is checked again just before it goes. Returns what was
    removed. Raises :class:`MirrorBusy` while a pass runs."""
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    if not os.path.isdir(_real_root(home)):
        return []
    root = _real_root(home)
    removed: list[str] = []
    with _pass_lock(home, wait=False):
        leftovers = set(_restore_leftovers(home, time.time(), STALE_TMP_S))
        for path in temp_files(home):
            beside_source = path in leftovers and _resolves_to_itself(path)
            if not (_inside(path, root) or beside_source):
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
                removed.append(path)
    return removed


def audit(home: Path | None = None) -> dict:
    """:func:`status` plus what ``swe doctor`` checks: ``untracked``
    (copies on disk the manifest does not know), ``missing`` (entries
    whose copy is gone) and ``stale_temps``. Read-only."""
    body = status(home)
    root = _real_root(home)
    entries = load_manifest(home)
    on_disk: set[str] = set()
    if os.path.isdir(root):
        for folder, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".jsonl"):
                    on_disk.add(os.path.relpath(os.path.join(folder, name), root))
    raw, _readable, _corrupt = _read_manifest_full(manifest_path(home))
    # A copy with an entry swe cannot read is not untracked: it is counted
    # in ``unreadable`` below.
    body["untracked"] = sorted(on_disk - set(entries) - set(raw))
    body["missing"] = sorted(rel for rel in entries if not os.path.isfile(os.path.join(root, rel)))
    body["stale_temps"] = temp_files(home)
    body["unreadable"] = len(set(raw) - set(entries))
    return body


# -- read side -----------------------------------------------------------


def _mirror_path(home: Path | None, rel: str) -> str:
    return os.path.join(os.path.realpath(transcripts_dir_for(home)), rel)


def _is_gone(entry: dict) -> bool:
    return not os.path.lexists(entry.get("source") or "")


def _group(rel: str, entry: dict) -> str:
    return entry.get("account_home") or ""


def listing_roots(harness: str, home: Path | None = None) -> list[tuple[str, str, set[str]]]:
    """Where the session listing should also look for ``harness``:
    ``(base_dir, account_home, skip_basenames)`` per mirror root.

    ``skip_basenames`` are the copies whose source still exists, so only
    sessions the harness has lost are parsed and listed; a copy missing
    from the manifest is not skipped, and the caller drops any session
    whose id is already live. Empty when nothing is mirrored."""
    if harness not in MIRRORED:
        return []
    home = home if home is not None else Path(os.path.expanduser("~"))
    base = transcripts_dir_for(home)
    if not base.is_dir():
        return []
    groups: list[tuple[str, str]] = [("", str(base / harness))]
    accounts = base / "accounts" / harness
    with contextlib.suppress(OSError):
        for child in sorted(os.scandir(accounts), key=lambda e: e.name):
            if child.is_dir():
                groups.append((str(swe_dir_for(home) / "accounts" / harness / child.name / "home"),
                               child.path))
    skip: dict[str, set[str]] = {account: set() for account, _ in groups}
    for rel, entry in load_manifest(home).items():
        if entry.get("harness") == harness and not _is_gone(entry):
            skip.setdefault(_group(rel, entry), set()).add(os.path.basename(rel))
    return [(path, account, skip.get(account, set()))
            for account, path in groups if os.path.isdir(path)]


def mirror_dir(harness: str, account_home: str = "", home: Path | None = None) -> Path | None:
    """The mirror directory holding ``harness`` transcripts for an
    account home (``""`` for the harness's own), when there is one."""
    if harness not in MIRRORED:
        return None
    home = home if home is not None else Path(os.path.expanduser("~"))
    base = transcripts_dir_for(home)
    path = (base / "accounts" / harness / Path(account_home).parent.name
            if account_home else base / harness)
    return path if path.is_dir() else None


def find_copy(harness: str, session_id: str, account_home: str = "",
              home: Path | None = None) -> Path | None:
    """The mirror copy a read should use for a session: among the copies
    whose file name is ``<session_id>.jsonl`` in the session's account
    folder, the one with the newest mtime (a copy keeps its source's), the
    same rule the listing applies when one id has several files. Uses the
    manifest, and walks the folder only when it holds no entry for the id
    (a lost manifest)."""
    base = mirror_dir(harness, account_home, home)
    if base is None or not session_id:
        return None
    # A linked account folder could lead into another account's copies:
    # it is read as if absent.
    expected = os.path.join(os.path.realpath(transcripts_dir_for(home)),
                            _group_rel(harness, account_home))
    if os.path.realpath(base) != expected or os.path.islink(expected):
        return None
    name = f"{Path(session_id).stem}.jsonl"
    found: list[Path] = []
    for rel in load_manifest(home):
        if os.path.basename(rel) == name:
            path = Path(_mirror_path(home, rel))
            if path.is_file() and path.is_relative_to(Path(os.path.realpath(base))):
                found.append(path)
    if not found:
        found = [p for p in base.rglob(name) if p.is_file()]
    if not found:
        return None
    return max(found, key=lambda p: (p.stat().st_mtime_ns, str(p)))


def status(home: Path | None = None) -> dict:
    """What the mirror holds and how its last pass went. Read-only. A
    manifest field in the wrong shape (a size that is not a number, a
    source that is not a path) counts as 0 or as not orphaned; it never
    raises."""
    def size(entry: dict) -> int:
        value = entry.get("mirror_size")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
                or value in (float("inf"), float("-inf")):
            return 0
        return int(value)

    entries = load_manifest(home)
    state = load_state(home)
    gone = [e for e in entries.values() if isinstance(e.get("source"), str) and _is_gone(e)]
    return {
        "enabled": is_enabled(home),
        "root": str(transcripts_dir_for(home)),
        "entries": len(entries),
        "bytes": sum(size(e) for e in entries.values()),
        "orphaned": len(gone),
        "orphaned_bytes": sum(size(e) for e in gone),
        "last_pass": state.get("last_pass"),
    }
