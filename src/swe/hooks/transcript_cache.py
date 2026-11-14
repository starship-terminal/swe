"""The hook backfill's parse cache: what each Claude transcript said.

``swe hooks stats`` and ``swe hooks log`` read every Claude Code
transcript in their window for hook runs, which takes seconds on a large
library. Most of those files have not changed since the last read, so
this keeps ``transcripts.parse_raw``'s answer for each one in
``~/.swe/cache/hooks/transcripts.sqlite``, keyed by the file's absolute
path and ``(size, mtime_ns, ctime_ns, inode)`` taken before the read.

Only what a file's own bytes say is kept: ``parse_raw`` leaves out what a
command runs, which plugin it came from and the repository a cwd is in,
and ``transcripts.finish`` and ``backfill`` work those out on every read.
The parse cache rule of ``sessions/parse_cache.py`` applies: a module
whose code decides a cached value is in ``_FINGERPRINT_MODULES``, and
editing one makes every row stale.

The store is SQLite in WAL mode with a busy timeout, so several readers
and writers (the CLI, the web hub's background reads, the app's swe) can
share it; a row is the version that wrote it plus the file it is about,
so two swe versions reading at once never wipe each other. It is bounded
(``CAP_BYTES`` of compressed rows; the least recently read go first), and
private (mode 600; the rows hold the first line of hook messages and
working folders). Any failure reads as a miss: the cache can only cost
time, never change an answer. ``SWE_HOOK_CACHE=0`` turns it off.
"""

from __future__ import annotations

import marshal
import os
import sqlite3
import sys
import time
import zlib
from pathlib import Path

from swe import paths as _paths

# Modules whose code decides what a cached row holds.
_FINGERPRINT_MODULES = (
    "swe.hooks.transcript_cache",
    "swe.hooks.transcripts",
    # command_hook (which drops a record that names no hook) and
    # reason_line decide what a row holds.
    "swe.hooks.events",
    "swe.hooks.shim_runtime",
)
CAP_BYTES = 64 * 1024 * 1024
_OFF_VALUES = ("0", "off", "false", "no")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    version TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    events BLOB NOT NULL,
    bytes INTEGER NOT NULL,
    used REAL NOT NULL,
    PRIMARY KEY (version, path)
)
"""


def cache_file_for(home: Path) -> Path:
    return _paths.hook_events_file_for(home).with_name("transcripts.sqlite")


def _version() -> str:
    """The deciding modules' digest, plus the Python that wrote the rows:
    they are ``marshal`` data, whose format is Python's own."""
    from swe.sessions.parse_cache import _modules_digest

    return (f"{_modules_digest(_FINGERPRINT_MODULES)}:"
            f"py{sys.version_info[0]}.{sys.version_info[1]}")


def _pack(events: list[dict]) -> bytes:
    """Rows as columns: the keys once, then one tuple of values per event
    (plain strings, numbers, booleans and None only), marshalled and
    compressed. Reading back is several times faster than JSON."""
    keys = sorted({k for e in events for k in e})
    rows = [tuple(e.get(k) for k in keys) for e in events]
    return zlib.compress(marshal.dumps((tuple(keys), rows)), 1)


def _unpack(blob: bytes) -> list[dict]:
    keys, rows = marshal.loads(zlib.decompress(blob))
    return [dict(zip(keys, row)) for row in rows]


def _signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


class TranscriptCache:
    """One read's view of the store: ``events`` answers per file, and
    ``close`` writes what was parsed and trims the store in one go."""

    def __init__(self, home: Path):
        self.path = cache_file_for(home)
        self.version = _version()
        self._db: sqlite3.Connection | None = None
        self._pending: list[tuple] = []
        self._hits: list[str] = []
        if os.environ.get("SWE_HOOK_CACHE", "").strip().lower() in _OFF_VALUES:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
            db = sqlite3.connect(str(self.path), timeout=2.0,
                                 isolation_level=None)
            db.execute("PRAGMA busy_timeout = 2000")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute(_SCHEMA)
            self._db = db
        except (OSError, sqlite3.Error):
            self._db = None

    def events(self, path: str, parse) -> list[dict]:
        """The raw events of ``path``: the stored ones when the file is
        unchanged, else ``parse(path)``'s, kept for the next read. A file
        that cannot be read gives none and nothing is stored."""
        try:
            st = os.stat(path)
        except OSError:
            return []
        sig = _signature(st)
        if self._db is not None:
            try:
                row = self._db.execute(
                    "SELECT size, mtime_ns, ctime_ns, inode, events FROM files "
                    "WHERE version = ? AND path = ?",
                    (self.version, path)).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None and tuple(row[:4]) == sig:
                try:
                    found = _unpack(row[4])
                    self._hits.append(path)
                    return found
                except (ValueError, TypeError, EOFError, zlib.error):
                    pass
        found = parse(path)
        if found is not None and self._db is not None:
            try:
                blob = _pack(found)
            except (TypeError, ValueError):
                return found
            self._pending.append((self.version, path, *sig, blob, len(blob)))
        return found if found is not None else []

    def close(self) -> None:
        """Store the new parses, mark the hits as read, trim to the cap."""
        db, self._db = self._db, None
        if db is None:
            return
        try:
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT OR REPLACE INTO files (version, path, size, mtime_ns, "
                "ctime_ns, inode, events, bytes, used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [row + (now,) for row in self._pending])
            db.executemany(
                "UPDATE files SET used = ? WHERE version = ? AND path = ?",
                [(now, self.version, p) for p in self._hits])
            if self._pending:
                self._trim(db)
            db.execute("COMMIT")
        except sqlite3.Error:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        finally:
            db.close()

    @staticmethod
    def _trim(db: sqlite3.Connection) -> None:
        total = 0
        drop = []
        for version, path, size in db.execute(
                "SELECT version, path, bytes FROM files ORDER BY used DESC"):
            total += size
            if total > CAP_BYTES:
                drop.append((version, path))
        db.executemany("DELETE FROM files WHERE version = ? AND path = ?", drop)
