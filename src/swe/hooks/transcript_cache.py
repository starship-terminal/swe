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

import json
import os
import sqlite3
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
# New parses are written, trimmed and their pages given back once this
# much is waiting, so one cold read of a whole library never grows the
# file past CAP_BYTES + BATCH_BYTES (plus SQLite's own pages).
BATCH_BYTES = 8 * 1024 * 1024
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
    from swe.sessions.parse_cache import _modules_digest

    return _modules_digest(_FINGERPRINT_MODULES)


def _pack(events: list[dict]) -> bytes:
    """Compressed JSON, one entry per field instead of per run: a value
    every run shares is stored once (``c``), a column of repeated strings
    as the distinct strings and an index per run (``d``/``i``), anything
    else as the list of values (``v``). A transcript's runs share their
    harness, session, folder and branch and repeat a few commands, so this
    reads back in a fraction of the time of a list of objects, and JSON's
    decoder raises on any bad byte rather than trusting the file."""
    keys = sorted({k for e in events for k in e})
    columns = []
    for key in keys:
        values = [e.get(key) for e in events]
        first = values[0] if values else None
        if all(v == first and type(v) is type(first) for v in values):
            columns.append({"c": first})
            continue
        if all(v is None or isinstance(v, str) for v in values):
            distinct = list(dict.fromkeys(values))
            if len(distinct) * 2 <= len(values):
                where = {v: n for n, v in enumerate(distinct)}
                columns.append({"d": distinct, "i": [where[v] for v in values]})
                continue
        columns.append({"v": values})
    return zlib.compress(json.dumps(
        {"n": len(events), "k": keys, "c": columns},
        separators=(",", ":")).encode("utf-8"), 1)


class BadRow(ValueError):
    """A stored row that is not what ``_pack`` wrote."""


def _column(spec, n: int) -> list:
    if not isinstance(spec, dict) or len(spec) not in (1, 2):
        raise BadRow("column")
    if "c" in spec and len(spec) == 1:
        return [spec["c"]] * n
    if "v" in spec and len(spec) == 1:
        values = spec["v"]
        if not isinstance(values, list) or len(values) != n:
            raise BadRow("column length")
        return values
    distinct, index = spec.get("d"), spec.get("i")
    if not isinstance(distinct, list) or not isinstance(index, list) \
            or len(index) != n:
        raise BadRow("dictionary column")
    if index and not (all(type(i) is int for i in index)
                      and 0 <= min(index) and max(index) < len(distinct)):
        raise BadRow("dictionary index")
    return [distinct[i] for i in index]


def _unpack(blob: bytes) -> list[dict]:
    """``_pack``'s events back; ``BadRow`` (or the decoder's own error) for
    anything else, so a damaged row is a miss and never a wrong answer."""
    data = json.loads(zlib.decompress(blob))
    if not isinstance(data, dict):
        raise BadRow("not an object")
    n, keys, specs = data.get("n"), data.get("k"), data.get("c")
    if type(n) is not int or n < 0 or not isinstance(keys, list) \
            or not all(isinstance(k, str) for k in keys) \
            or not isinstance(specs, list) or len(specs) != len(keys):
        raise BadRow("no columns")
    if n == 0:
        # Most transcripts hold no hook run at all: that is an answer too.
        return []
    columns = dict(zip(keys, (_column(spec, n) for spec in specs)))
    ts = columns.get("ts")
    command = columns.get("_command", ())
    if ts is None or not all(type(t) in (int, float) for t in ts) \
            or not all(isinstance(c, str) for c in command):
        raise BadRow("field type")
    return [dict(zip(keys, row)) for row in zip(*(columns[k] for k in keys))]


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
        self._pending_bytes = 0
        self._hits: list[str] = []
        if os.environ.get("SWE_HOOK_CACHE", "").strip().lower() in _OFF_VALUES:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
            db = sqlite3.connect(str(self.path), timeout=2.0,
                                 isolation_level=None)
        except (OSError, sqlite3.Error):
            return
        try:
            db.execute("PRAGMA busy_timeout = 2000")
            # Set before the first table exists, it lets a trim hand freed
            # pages back to the disk (``PRAGMA incremental_vacuum``).
            db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute(_SCHEMA)
            if db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                # A store made before auto-vacuum was set: the setting only
                # takes with a rebuild. Busy (another reader) leaves it for
                # a later read.
                try:
                    db.execute("VACUUM")
                except sqlite3.Error:
                    pass
            self._db = db
        except (OSError, sqlite3.Error):
            # Not a database (damaged, another file): read uncached.
            db.close()
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
                except Exception:  # noqa: BLE001 - any damage is a miss
                    pass
        found = parse(path)
        if found is not None and self._db is not None:
            try:
                blob = _pack(found)
            except (TypeError, ValueError):
                return found
            # ``used`` orders rows by when they were read, newest last, so a
            # trim inside one large read drops its oldest parses first.
            self._pending.append(
                (self.version, path, *sig, blob, len(blob), time.time()))
            self._pending_bytes += len(blob)
            if self._pending_bytes >= BATCH_BYTES:
                self._flush(self._db)
        return found if found is not None else []

    def _flush(self, db: sqlite3.Connection) -> None:
        """Write the waiting parses and the hits, trim to the cap and give
        the freed pages back, in one transaction. Fails to a no-op."""
        pending, self._pending, self._pending_bytes = self._pending, [], 0
        hits, self._hits = self._hits, []
        try:
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT OR REPLACE INTO files (version, path, size, mtime_ns, "
                "ctime_ns, inode, events, bytes, used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", pending)
            db.executemany(
                "UPDATE files SET used = ? WHERE version = ? AND path = ?",
                [(now, self.version, p) for p in hits])
            if pending:
                self._trim(db)
                # It frees one page per step: run it to the end.
                db.execute("PRAGMA incremental_vacuum").fetchall()
            db.execute("COMMIT")
        except sqlite3.Error:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        """Store the new parses, mark the hits as read, trim to the cap and
        fold the write-ahead log back so the file shrinks."""
        db, self._db = self._db, None
        if db is None:
            return
        try:
            wrote = bool(self._pending)
            self._flush(db)
            if wrote:
                try:
                    # Another reader may hold the log; then it shrinks on a
                    # later close instead.
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
