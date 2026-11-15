"""Persistent per-file parse cache for session listings.

A listing reads every transcript on disk, but between two listings almost
none of them change. This cache keeps what a parser extracted from one file,
keyed by the file's absolute path, and hands it back while the file's stat
signature is unchanged, so an unchanged file costs one ``stat`` instead of a
read and a JSON parse per line.

Rules that keep it from ever showing a stale session:

- **The signature is taken before the read.** A file written between the
  ``stat`` and the read is stored under the older signature, so the next run
  sees a mismatch and reads it again. The other order would pin old content
  to a new signature for good.
- **The signature is (size, mtime_ns, ctime_ns, inode).** ``ctime`` moves on
  every content change and cannot be set back from userland, so a write that
  restores the old mtime (``touch -m``, ``cp -p``) still invalidates; an
  atomic replace changes the inode.
- **A file written just now is not stored.** Two writes inside one clock
  tick (a few ms on a coarse Linux kernel clock, 1 s or 2 s on some
  filesystems) share a ctime and an mtime, and a same-size rewrite with the
  mtime put back then matches the stored signature in all four fields; no
  stat can tell it apart. So a value is stored only once the file's ctime
  is at least ``_RACY_WINDOW_NS`` older than the lookup (2 s;
  ``SWE_CACHE_RACY_WINDOW_MS`` overrides it, ``0`` for a test that spawns
  ``swe`` on files it just wrote), the rule git's
  index uses for "racily clean" entries. A file that fresh is read again
  on the next listing, which a live session's growing transcript needs
  anyway.
- **Only what a file says is cached.** Anything a parser derives from other
  state (a sidecar index, a sqlite store, whether a directory exists, the
  clock) is applied after the lookup, on every run, or keyed by that
  sidecar's own path.
- **Any code change discards it.** The header holds a format version, the
  package version and a hash of the modules whose logic produced the
  values, so an upgrade or an edit cannot serve results from older code.
- **Any read problem is a miss.** A missing, corrupt or foreign cache file
  reads as empty; an entry with the wrong shape is ignored.

Caching only happens inside ``activate()``, which the aggregator opens around
a listing. Parsers called anywhere else compute every time, exactly as
before. When the block closes, entries for files a harness's parser no
longer visited are pruned, but only for harnesses whose parser ran to
completion, so an ``--agent`` filtered run leaves the others alone.

``standalone()`` opens the same kind of store in a file of its own, with
its own deciding modules, for a caller outside a listing (the status
column's tail reads). Its entries go only once their file is gone.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from swe.paths import atomic_write_text, session_parse_cache_file_for

# Bump when the on-disk layout of an entry changes.
FORMAT_VERSION = 1

# Modules whose code decides what a cached value contains. Editing any of
# them changes the fingerprint and discards every entry.
_FINGERPRINT_MODULES = (
    "swe.sessions.parse_cache",
    "swe.sessions.parsers",
    "swe.sessions.engines.jsonl_engine",
    "swe.sessions.engines.common",
    "swe.sessions.engines.json_engine",
)

_local = threading.local()
_fingerprint: str | None = None
_digests: dict[tuple[str, ...], str] = {}


def _modules_digest(modules: tuple[str, ...]) -> str:
    """Format version, package version and a hash of ``modules``' source."""
    cached = _digests.get(modules)
    if cached is not None:
        return cached
    import importlib

    from swe import __version__

    digest = hashlib.sha256()
    for name in modules:
        try:
            module = importlib.import_module(name)
            with open(module.__file__, "rb") as fh:
                digest.update(fh.read())
        except Exception:
            # No readable source (a zipapp, a frozen build): the package
            # version alone still separates releases.
            digest.update(f"nosrc:{name}".encode())
    value = _digests[modules] = (
        f"{FORMAT_VERSION}:{__version__}:{digest.hexdigest()[:16]}"
    )
    return value


def _source_fingerprint() -> str:
    """The parse cache's version: its deciding modules' digest."""
    global _fingerprint
    if _fingerprint is None:
        _fingerprint = _modules_digest(_FINGERPRINT_MODULES)
    return _fingerprint


def _is_valid(valid, value) -> bool:
    try:
        return bool(valid(value))
    except Exception:
        return False


def _signature(st: os.stat_result) -> list[int]:
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


# A file changed within this long of a lookup may still be rewritten inside
# the same timestamp tick, so its signature proves nothing yet. Wider than
# the coarsest filesystem tick in use (2 s on FAT).
_RACY_WINDOW_NS = 2_000_000_000


def _racy_window_ns() -> int:
    raw = os.environ.get("SWE_CACHE_RACY_WINDOW_MS", "").strip()
    try:
        return max(0, int(raw)) * 1_000_000 if raw else _RACY_WINDOW_NS
    except ValueError:
        return _RACY_WINDOW_NS


def _is_racy(st: os.stat_result, window_ns: int) -> bool:
    """True while a rewrite could still hide inside ``st``'s timestamp tick.

    A ctime in the future (a clock that stepped back) counts as racy.
    """
    return not (st.st_ctime_ns <= time.time_ns() - window_ns)


class _Store:
    def __init__(self, path: Path, fingerprint: str | None = None) -> None:
        self.path = path
        self.fingerprint = fingerprint or _source_fingerprint()
        self.entries: dict[str, dict[str, list]] = {}
        self.touched: dict[str, set[str]] = {}
        # Harnesses whose parser ran to completion in this block. Only their
        # namespaces are pruned, so an --agent filtered listing, or a parser
        # that crashed, leaves everyone else's entries alone.
        self.ran: set[str] = set()
        self.dirty = False
        self.read_failed = False
        self.racy_ns = _racy_window_ns()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict) or data.get("version") != self.fingerprint:
            return
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return
        self.entries = {
            ns: files
            for ns, files in entries.items()
            if isinstance(ns, str) and isinstance(files, dict)
        }

    def lookup(self, namespace: str, path: str, st: os.stat_result, compute, valid):
        sig = _signature(st)
        racy = _is_racy(st, self.racy_ns)
        self.touched.setdefault(namespace, set()).add(path)
        files = self.entries.setdefault(namespace, {})
        entry = files.get(path)
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and entry[0] == sig
            # A stored entry was settled when it was written, so this only
            # bites when the clock stepped back or the window grew: then the
            # signature proves nothing, and the file is read again.
            and not racy
            and (valid is None or _is_valid(valid, entry[1]))
        ):
            return entry[1]
        self.read_failed = False
        try:
            value = compute()
        finally:
            failed, self.read_failed = self.read_failed, False
        # Hand back exactly what a later hit will load, so a cold and a
        # warm run can never differ by type (a tuple read back as a list).
        try:
            stored = json.loads(json.dumps(value))
        except (TypeError, ValueError):
            failed, stored = True, value
        if failed:
            # A read that failed (the file vanished, a transient EIO or
            # EMFILE) would otherwise pin its empty result to a signature
            # that never changes; leave it to be read again next time.
            if files.pop(path, None) is not None:
                self.dirty = True
            return stored
        if racy:
            # Computed fresh and handed back, but not remembered: see the
            # module docstring.
            if files.pop(path, None) is not None:
                self.dirty = True
            return stored
        files[path] = [sig, stored]
        self.dirty = True
        return stored

    def prune(self) -> None:
        """Drop entries the harness's parser ran past without visiting.

        A namespace is ``<harness>:<what>``. A file its parser no longer
        lists (deleted, moved, no longer the newest copy) is not visited,
        and goes, even when nothing at all was visited.
        """
        for namespace, files in self.entries.items():
            if namespace.split(":", 1)[0] not in self.ran or not files:
                continue
            seen = self.touched.get(namespace, set())
            stale = [p for p in files if p not in seen]
            for p in stale:
                del files[p]
            if stale:
                self.dirty = True

    def save(self) -> None:
        self.prune()
        if not self.dirty:
            return
        payload = {"version": self.fingerprint, "entries": self.entries}
        try:
            # Titles are first prompts; keep the file owner-only.
            atomic_write_text(
                self.path,
                json.dumps(payload, separators=(",", ":")),
                private=True,
            )
        except Exception:
            # A cache that cannot be written only costs the next run time.
            pass


# How often a ``_FileStore`` sweeps for entries whose file is gone, per
# cache file, in seconds; and when each cache file was last swept.
_SWEEP_INTERVAL_S = 300.0
_last_sweep: dict[Path, float] = {}


class _FileStore(_Store):
    """A store whose entries last as long as their files do.

    For a caller that looks up whatever subset of files it was handed
    (``swe session 10``, one remote row), so "not visited this run" says
    nothing. An entry goes once its file no longer exists, checked only
    when the cache is being rewritten anyway, and at most once per
    ``_SWEEP_INTERVAL_S`` per cache file in this process: the sweep is one
    ``stat`` per entry (about 5 ms for 1,000 entries), and the hub's
    status probes rewrite this file up to once a second while a session
    is busy. A dead file's entry only costs space until the next sweep.
    """

    def prune(self) -> None:
        if not self.dirty:
            return
        now = time.monotonic()
        last = _last_sweep.get(self.path)
        if last is not None and now - last < _SWEEP_INTERVAL_S:
            return
        _last_sweep[self.path] = now
        for namespace, files in self.entries.items():
            seen = self.touched.get(namespace, set())
            for p in [p for p in files if p not in seen and not os.path.exists(p)]:
                del files[p]


def _active() -> _Store | None:
    return getattr(_local, "store", None)


@contextmanager
def activate(home: Path | None = None):
    """Use and persist the parse cache for the parsers run inside.

    Re-entrant: only the outermost block loads and saves. ``home`` defaults
    to the current ``$HOME``, read when the block opens.
    """
    outer = _active()
    if outer is not None:
        yield outer
        return
    try:
        store = _Store(session_parse_cache_file_for(home))
    except Exception:
        # No resolvable home: list without a cache rather than fail.
        yield None
        return
    store.load()
    _local.store = store
    try:
        yield store
    finally:
        _local.store = None
        store.save()


@contextmanager
def standalone(path_for: Callable[[], Path], modules: tuple[str, ...]):
    """A cache file of its own, for ``lookup_in``, outside ``activate()``.

    ``path_for()`` names the file; ``modules`` are the ones whose code
    decides the cached values, so editing any of them discards every
    entry. Entries are pruned only once their file is gone. Yields
    ``None`` (look-ups then just compute) when the path cannot be
    resolved.
    """
    try:
        store = _FileStore(path_for(), _modules_digest(modules))
    except Exception:
        yield None
        return
    store.load()
    try:
        yield store
    finally:
        store.save()


def note_read_error() -> None:
    """Keep the lookup in progress from storing its result.

    A ``compute`` calls this when opening or reading its file raised
    ``OSError``: what it returns then says nothing about the file, only
    about this attempt. Errors that come from the bytes themselves
    (malformed JSON, bad UTF-8) are a property of the file and stay
    cacheable. Outside a lookup this does nothing.
    """
    store = _active()
    if store is not None:
        store.read_failed = True


def mark_ran(harness: str) -> None:
    """Record that ``harness``'s parser finished, so its stale entries prune."""
    store = _active()
    if store is not None:
        store.ran.add(harness)


def lookup(
    namespace: str,
    path: str,
    compute: Callable[[], Any],
    st: os.stat_result | None = None,
    valid: Callable[[Any], bool] | None = None,
) -> Any:
    """``compute()`` for ``path``, reused while the file is unchanged.

    ``namespace`` is ``<harness>:<what>``, the harness being the parser's
    registry name, which is what ``mark_ran`` prunes by.

    ``compute`` must depend only on the file's own bytes and return plain
    JSON values. Both a hit and a miss come back through a JSON round
    trip, so a tuple always returns as a list; a value JSON cannot hold is
    returned as is and not stored. ``st`` is the caller's ``os.stat(path)`` when
    it already has one; it has to be taken before the file is read.
    ``valid`` checks a stored value's shape; one it rejects (a hand-edited
    or damaged entry) is a miss, never an exception in the parser.
    Outside ``activate()``, or when the file cannot be stat'ed, this is
    just ``compute()``.
    """
    return lookup_in(_active(), namespace, path, compute, st, valid)


def lookup_in(
    store: _Store | None,
    namespace: str,
    path: str,
    compute: Callable[[], Any],
    st: os.stat_result | None = None,
    valid: Callable[[Any], bool] | None = None,
) -> Any:
    """``lookup`` against ``store`` (one from ``standalone``); ``None`` computes."""
    if store is None:
        return compute()
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return compute()
    return store.lookup(namespace, os.path.abspath(path), st, compute, valid)
