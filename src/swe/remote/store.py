"""``~/.swe/data/remote/remote.sqlite``: the hub's session store (G6).

Four tables: ``sessions`` (every SWE-owned session the hub knows),
``keepers`` (the keeper process holding each live one), ``launches``
(every ``POST /v1/sessions``) and ``commands`` (every write a client
sent, with its receipt). The spec is ``docs/specs/G6-sqlite-store.md``.

Connection model: one :class:`Store` holds one connection behind an
``RLock``, so the hub's threads share it safely. Every write runs in
``BEGIN IMMEDIATE``, which takes SQLite's write lock before the first
read, so a check-then-insert (``record_command``) is one step even
against another ``Store`` (another connection, another process), and
``busy_timeout`` makes the loser wait instead of failing with
``database is locked``.

Only the hub writes the file. Anything else reads it through its own
connection (WAL lets readers run beside the writer), and never through
a copy: ``backup`` is the one way to copy it, because a commit can sit
in ``remote.sqlite-wal`` for a while after it returns.

The store is user data, not a cache: nothing here ever deletes or
rebuilds the file. A file this build does not understand (a newer
schema, another program's database, not a database at all) is refused
with :class:`StoreError` and left exactly as it was.

No secrets go in: payloads, options and results are checked against
allowlists and a key-name denylist, so a device token, bearer value or
API key is refused before it reaches the disk.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from swe.paths import remote_dir_for
from swe.remote.adapters.events import RECEIPT_STATUSES, SESSION_STATES

BASENAME = "remote.sqlite"
TABLES = ("sessions", "keepers", "launches", "commands")

# ``PRAGMA application_id``: "SWER". Tells this store from any other
# SQLite file someone (or a bug) put at the same path.
APPLICATION_ID = 0x53574552

# How long the hub holds a command for a machine that is asleep
# (G1 spec, section 3): after this the command is rejected as expired.
HOLD_S = 24 * 3600.0

# How long a finished command is kept. A retry with the same
# command_id is caught only while its row exists, so this must outlast
# any client's retry window, the hold included.
RETAIN_S = 7 * 86_400.0

BUSY_TIMEOUT_MS = 5000

# Statuses a stored command can have. ``duplicate`` is only ever an
# answer, never stored: it means "seen before, here is what happened".
STORED_STATUSES = RECEIPT_STATUSES - {"duplicate"}

# Where a stored status may move. Everything else is refused.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "held": frozenset({"accepted", "queued", "rejected"}),
    "queued": frozenset({"accepted", "rejected"}),
    "accepted": frozenset(),
    "rejected": frozenset(),
}

CONTROL_MODES = frozenset({"full", "read_only"})
KEEPER_STATES = frozenset({"starting", "live", "exited", "lost"})
LAUNCH_STATES = frozenset({"pending", "started", "failed", "untracked"})

# Launch and session options: picks, never credentials.
OPTION_KEYS = frozenset({"model", "effort", "permission_mode", "plan_mode",
                         "yolo", "provider"})
# A command's result: the ``command.result`` event's data fields.
RESULT_KEYS = frozenset({"op", "ok", "note", "degraded", "cancelled",
                         "request_id", "launch_id"})

# The wire rule for client_id (protocol._CLIENT_ID_RE), kept for
# command_id so an old client's id is always a valid new one.
_COMMAND_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_OP_RE = re.compile(r"[a-z][a-z0-9_.]{0,31}")
_DEVICE_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")
# A payload key that names a credential is refused at any depth.
_SECRET_KEY_RE = re.compile(
    r"token|bearer|secret|passw|credential|cookie|authoriz|api_?key|"
    r"private_?key", re.IGNORECASE)
MAX_PAYLOAD_BYTES = 1 << 20


class StoreError(Exception):
    """The store cannot be opened or written. The message is for a
    person: it says which file and what to do."""


class StoreNewer(StoreError):
    """The file was written by a newer swe (a higher ``user_version``).
    It is left untouched; update swe to open it."""


class StoreConflict(StoreError):
    """A row that must be unique already exists (a launch id, or a
    session's harness id under another key)."""


def store_path(home: Path | None = None) -> Path:
    """Where the store lives: ``data/remote/remote.sqlite``."""
    return remote_dir_for(home) / BASENAME


# ---------------------------------------------------------------------
# Rows


@dataclass(frozen=True)
class Session:
    key: str
    harness: str
    harness_session_id: str
    machine: str
    cwd: str
    state: str
    control: str
    control_reason: str | None
    adapter: str | None
    options: dict
    epoch: int
    last_seq: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Keeper:
    slug: str
    session: str
    pid: int
    socket_path: str
    ipc_version: int
    state: str
    note: str | None
    started_at: float
    last_seen: float


@dataclass(frozen=True)
class Launch:
    launch_id: str
    command_id: str | None
    session: str | None
    machine: str
    harness: str
    cwd: str
    options: dict
    state: str
    note: str | None
    created_at: float
    settled_at: float | None


@dataclass(frozen=True)
class Command:
    id: int  # insertion order: held commands deliver in this order
    command_id: str
    session: str
    machine: str
    op: str
    payload: dict
    device: str | None
    status: str
    reason: str | None
    result: dict | None
    received_at: float
    updated_at: float
    expires_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class Receipt:
    """What ``record_command`` answers.

    ``status`` is what to tell the client now. On a first call it is
    the status stored. On a repeat it is ``duplicate``, ``original`` is
    the stored status as it stands now (a held command delivered since
    answers ``accepted``), and ``result`` is the stored result if the
    command has finished. ``conflict`` marks a repeat id carrying a
    different command: answered ``rejected``, nothing stored.
    """

    command_id: str
    status: str
    op: str
    reason: str | None = None
    original: str | None = None
    result: dict | None = None
    duplicate: bool = False
    conflict: bool = False


# ---------------------------------------------------------------------
# Migrations
#
# Append-only. A shipped step is never edited: the next schema change is
# a new function and a new entry. Each step runs inside the one
# ``BEGIN IMMEDIATE`` transaction that also sets ``user_version``, so a
# failed step leaves the file at its old version with its old tables.
# Execute statements one at a time: ``executescript`` commits first.


def _v1(conn: sqlite3.Connection) -> None:
    for sql in (
        f"PRAGMA application_id = {APPLICATION_ID}",
        """CREATE TABLE sessions(
             key TEXT PRIMARY KEY,
             harness TEXT NOT NULL,
             harness_session_id TEXT NOT NULL,
             machine TEXT NOT NULL,
             cwd TEXT NOT NULL DEFAULT '',
             state TEXT NOT NULL,
             control TEXT NOT NULL DEFAULT 'full',
             control_reason TEXT,
             adapter TEXT,
             options TEXT NOT NULL DEFAULT '{}',
             epoch INTEGER NOT NULL DEFAULT 0,
             last_seq INTEGER NOT NULL DEFAULT 0,
             created_at REAL NOT NULL,
             updated_at REAL NOT NULL,
             UNIQUE(machine, harness, harness_session_id))""",
        """CREATE TABLE keepers(
             slug TEXT PRIMARY KEY,
             session_key TEXT NOT NULL,
             pid INTEGER NOT NULL,
             socket_path TEXT NOT NULL,
             ipc_version INTEGER NOT NULL,
             state TEXT NOT NULL,
             note TEXT,
             started_at REAL NOT NULL,
             last_seen REAL NOT NULL)""",
        "CREATE INDEX keepers_session ON keepers(session_key)",
        """CREATE TABLE launches(
             launch_id TEXT PRIMARY KEY,
             command_id TEXT UNIQUE,
             session_key TEXT,
             machine TEXT NOT NULL,
             harness TEXT NOT NULL,
             cwd TEXT NOT NULL,
             options TEXT NOT NULL DEFAULT '{}',
             state TEXT NOT NULL,
             note TEXT,
             created_at REAL NOT NULL,
             settled_at REAL)""",
        """CREATE TABLE commands(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             command_id TEXT NOT NULL UNIQUE,
             session_key TEXT NOT NULL DEFAULT '',
             machine TEXT NOT NULL DEFAULT '',
             op TEXT NOT NULL,
             payload TEXT NOT NULL,
             device TEXT,
             status TEXT NOT NULL,
             reason TEXT,
             result TEXT,
             received_at REAL NOT NULL,
             updated_at REAL NOT NULL,
             expires_at REAL,
             finished_at REAL)""",
        "CREATE INDEX commands_held ON commands(machine, id) "
        "WHERE status = 'held'",
        "CREATE INDEX commands_open ON commands(id) WHERE result IS NULL",
        "CREATE INDEX commands_finished ON commands(finished_at) "
        "WHERE result IS NOT NULL",
    ):
        conn.execute(sql)


# (version, step): step N takes the file from version N-1 to N.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _v1),
)
SCHEMA_VERSION = len(MIGRATIONS)


# ---------------------------------------------------------------------
# Value checks


def _canon(value: Any) -> str:
    """One spelling per value, so two payloads compare as text."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not strict JSON: {exc}") from None


def _no_secrets(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{where}: keys must be strings")
            if _SECRET_KEY_RE.search(k):
                raise ValueError(
                    f"{where}: {k!r} looks like a credential; the store "
                    "keeps no secrets")
            _no_secrets(v, f"{where}.{k}")
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item, where)


def _payload(payload: dict | None) -> str:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    _no_secrets(payload, "payload")
    text = _canon(payload)
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload is over 1 MiB")
    return text


def _options(options: dict | None) -> str:
    if options is None:
        return "{}"
    if not isinstance(options, dict):
        raise ValueError("options must be a JSON object")
    for k, v in options.items():
        if k not in OPTION_KEYS:
            raise ValueError(
                f"options: unknown key {k!r} (allowed: "
                f"{', '.join(sorted(OPTION_KEYS))})")
        if not isinstance(v, (str, bool)):
            raise ValueError(f"options.{k} must be a string or a bool")
    return _canon(options)


def _result(result: dict, op: str) -> str:
    if not isinstance(result, dict):
        raise ValueError("result must be a JSON object")
    unknown = set(result) - RESULT_KEYS
    if unknown:
        raise ValueError(f"result: unknown keys {sorted(unknown)}")
    if not isinstance(result.get("ok"), bool):
        raise ValueError("result.ok must be a bool")
    return _canon({"op": op, **{k: v for k, v in result.items()
                                if v is not None}})


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _COMMAND_ID_RE.fullmatch(value):
        raise ValueError(f"{what} must be 1-64 chars of [A-Za-z0-9_-]")
    return value


def _check_text(value: Any, what: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) \
            or "\x00" in value:
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _check_in(value: Any, allowed: frozenset, what: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{what} must be one of {sorted(allowed)}")
    return value


def command_id_of(body: dict) -> str | None:
    """The command id a request carries: ``command_id``, or
    ``client_id`` from a client that predates it (the same value under
    its old name). None when it has neither; ValueError when the two
    disagree or the id is malformed."""
    cid = body.get("command_id")
    alias = body.get("client_id")
    if cid in (None, ""):
        cid = alias
    elif alias not in (None, "") and alias != cid:
        raise ValueError("command_id and client_id differ")
    if cid in (None, ""):
        return None
    return _check_id(cid, "command_id")


# ---------------------------------------------------------------------
# The store


def _private_file(path: Path) -> None:
    """Create ``path`` empty at 0600 if it is missing. SQLite gives the
    ``-wal`` and ``-shm`` files the mode of the database file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _tighten(path: Path) -> None:
    for name in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if name.stat().st_mode & 0o077:
                os.chmod(name, 0o600)
        except OSError:
            pass


_MAGIC = b"SQLite format 3\x00"


def _header(path: Path) -> None:
    """Refuse from the file's own header bytes, before SQLite opens it:
    even a read-only open of a WAL file leaves ``-wal`` and ``-shm``
    files beside it. ``application_id`` is at byte 68 and
    ``user_version`` at byte 60. A bump still only in another
    process's ``-wal`` is caught by :func:`_probe` instead."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(100)
    except OSError as exc:
        raise StoreError(f"{path}: cannot read ({exc})") from None
    if not head:
        return  # new and empty
    if len(head) < 100 or not head.startswith(_MAGIC):
        raise StoreError(
            f"{path} is not a SQLite database; it was left as it is. "
            "Move it aside to start a new store")
    version = int.from_bytes(head[60:64], "big", signed=True)
    app = int.from_bytes(head[68:72], "big", signed=True)
    if (app, version) != (0, 0):
        _refusal(path, app, version, None)


def _probe(path: Path) -> tuple[int, int, int]:
    """``(application_id, user_version, tables)`` read through a
    read-only connection, which can neither write the file nor
    checkpoint a WAL into it when it closes."""
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise StoreError(f"{path}: cannot open ({exc})") from None
    try:
        app = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise StoreError(
            f"{path} is not a readable SQLite database ({exc}); it was "
            "left as it is. Move it aside to start a new store") from None
    finally:
        conn.close()
    return app, version, tables


def _refusal(path: Path, app: int, version: int,
             tables: int | None) -> None:
    """Raise if this build must not touch the file. ``tables`` is None
    when only the header was read."""
    if version == 0 and app == 0 and tables == 0:
        return  # a new, empty file
    if app != APPLICATION_ID:
        raise StoreError(
            f"{path} is not a swe remote store (application_id {app:#x}); "
            "it was left as it is. Move it aside to start a new store")
    if version > len(MIGRATIONS):
        raise StoreNewer(
            f"{path} holds schema version {version}, and this swe knows "
            f"versions up to {len(MIGRATIONS)}. It was left as it is; "
            "update swe to use it")


class Store:
    """One connection to the store, safe to share between threads.

    ``clock`` gives the time for every timestamp and deadline (tests
    pass a fake one). ``Store`` is a context manager; ``close`` is
    idempotent.
    """

    def __init__(self, path: Path | str,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_file(self.path)
        # The version check comes before any pragma: turning WAL on
        # writes the header, and a refused file must stay byte-identical.
        _header(self.path)
        _refusal(self.path, *_probe(self.path))
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None, check_same_thread=False)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StoreError(
                    f"{self.path}: SQLite would not turn on WAL (got "
                    f"{mode}); keep the store on a local disk")
            # FULL: a receipt that returned is on disk, so a power cut
            # cannot forget a command and apply its retry a second time.
            conn.execute("PRAGMA synchronous = FULL")
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        _tighten(self.path)

    @classmethod
    def open(cls, home: Path | None = None,
             clock: Callable[[], float] = time.time) -> "Store":
        """The store under ``home``'s swe data folder."""
        return cls(store_path(home), clock=clock)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- plumbing -----------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        target = len(MIGRATIONS)
        if conn.execute("PRAGMA user_version").fetchone()[0] == target:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Read again under the write lock: another process may have
            # migrated (or a newer swe written) since the probe.
            app = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            _refusal(self.path, app, version, tables)
            for number, step in MIGRATIONS[version:]:
                if number != version + 1:
                    raise StoreError(
                        f"migration {number} is out of order after "
                        f"{version}")
                step(conn)
                version = number
            conn.execute(f"PRAGMA user_version = {target}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError(f"{self.path}: the store is closed")
        return self._conn

    def _write(self) -> "_Tx":
        return _Tx(self)

    def _read(self, sql: str, args: Sequence = ()) -> list[tuple]:
        with self._lock:
            return self._db().execute(sql, args).fetchall()

    # -- sessions -----------------------------------------------------

    def put_session(
        self, key: str, *, harness: str, harness_session_id: str,
        machine: str, cwd: str = "", state: str = "starting",
        control: str = "full", control_reason: str | None = None,
        adapter: str | None = None, options: dict | None = None,
    ) -> Session:
        """Insert a session, or replace every given field of an existing
        one (``created_at``, ``epoch`` and ``last_seq`` are kept)."""
        _check_text(key, "key")
        _check_text(harness, "harness")
        _check_text(harness_session_id, "harness_session_id")
        _check_text(machine, "machine")
        _check_text(cwd, "cwd", empty=True)
        _check_in(state, SESSION_STATES, "state")
        control, control_reason = _control(control, control_reason)
        opts = _options(options)
        now = self._clock()
        with self._write() as db:
            try:
                db.execute(
                    """INSERT INTO sessions(key, harness, harness_session_id,
                         machine, cwd, state, control, control_reason,
                         adapter, options, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         harness = excluded.harness,
                         harness_session_id = excluded.harness_session_id,
                         machine = excluded.machine, cwd = excluded.cwd,
                         state = excluded.state, control = excluded.control,
                         control_reason = excluded.control_reason,
                         adapter = excluded.adapter,
                         options = excluded.options,
                         updated_at = excluded.updated_at""",
                    (key, harness, harness_session_id, machine, cwd, state,
                     control, control_reason, adapter, opts, now, now))
            except sqlite3.IntegrityError:
                raise StoreConflict(
                    f"{harness} session {harness_session_id} on {machine} "
                    "is already stored under another key") from None
        found = self.get_session(key)
        assert found is not None
        return found

    _SESSION_FIELDS = frozenset({"cwd", "state", "control",
                                 "control_reason", "adapter", "options",
                                 "epoch", "last_seq"})

    def update_session(self, key: str, **fields: Any) -> bool:
        """Change some fields of one session; False when it is not
        stored. ``control="full"`` clears the reason; ``read_only``
        needs one."""
        unknown = set(fields) - self._SESSION_FIELDS
        if unknown:
            raise ValueError(f"update_session: unknown fields "
                             f"{sorted(unknown)}")
        sets: dict[str, Any] = {}
        if "cwd" in fields:
            sets["cwd"] = _check_text(fields["cwd"], "cwd", empty=True)
        if "state" in fields:
            sets["state"] = _check_in(fields["state"], SESSION_STATES,
                                      "state")
        if "control" in fields:
            sets["control"], sets["control_reason"] = _control(
                fields["control"], fields.get("control_reason"))
        elif "control_reason" in fields:
            raise ValueError("control_reason goes with control")
        if "adapter" in fields:
            adapter = fields["adapter"]
            if adapter is not None:
                _check_text(adapter, "adapter")
            sets["adapter"] = adapter
        if "options" in fields:
            sets["options"] = _options(fields["options"])
        for name in ("epoch", "last_seq"):
            if name in fields:
                value = fields[name]
                if not isinstance(value, int) or isinstance(value, bool) \
                        or value < 0:
                    raise ValueError(f"{name} must be a non-negative int")
                sets[name] = value
        if not sets:
            return self.get_session(key) is not None
        sets["updated_at"] = self._clock()
        cols = ", ".join(f"{c} = ?" for c in sets)
        with self._write() as db:
            cur = db.execute(f"UPDATE sessions SET {cols} WHERE key = ?",
                             (*sets.values(), key))
            return cur.rowcount == 1

    def note_seq(self, key: str, seq: int) -> bool:
        """Move ``last_seq`` forward to ``seq``; never back. False when
        the session is not stored."""
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("seq must be a non-negative int")
        with self._write() as db:
            cur = db.execute(
                "UPDATE sessions SET last_seq = MAX(last_seq, ?), "
                "updated_at = ? WHERE key = ?", (seq, self._clock(), key))
            return cur.rowcount == 1

    def get_session(self, key: str) -> Session | None:
        rows = self._read(f"SELECT {_SESSION_COLS} FROM sessions "
                          "WHERE key = ?", (key,))
        return _session(rows[0]) if rows else None

    def list_sessions(self, *, machine: str | None = None,
                      states: Sequence[str] | None = None) -> list[Session]:
        """Sessions oldest first, optionally of one machine or in some
        states."""
        where, args = _filters(("machine", machine), ("state", states))
        return [_session(r) for r in self._read(
            f"SELECT {_SESSION_COLS} FROM sessions{where} "
            "ORDER BY created_at, key", args)]

    # -- keepers ------------------------------------------------------

    def put_keeper(self, slug: str, *, session: str, pid: int,
                   socket_path: str, ipc_version: int,
                   state: str = "starting", note: str | None = None
                   ) -> Keeper:
        """Record a keeper that just started. An existing row for the
        slug is a previous keeper of that session and is replaced."""
        _check_text(slug, "slug")
        _check_text(session, "session")
        _positive(pid, "pid")
        _positive(ipc_version, "ipc_version")
        if not isinstance(socket_path, str) or not socket_path.startswith(
                "/"):
            raise ValueError("socket_path must be an absolute path")
        _check_in(state, KEEPER_STATES, "state")
        now = self._clock()
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO keepers(slug, session_key, pid, "
                "socket_path, ipc_version, state, note, started_at, "
                "last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                (slug, session, pid, socket_path, ipc_version, state, note,
                 now, now))
        found = self.get_keeper(slug)
        assert found is not None
        return found

    def touch_keeper(self, slug: str, *, state: str | None = None,
                     note: str | None = None) -> bool:
        """The hub heard from the keeper: stamp ``last_seen`` and, when
        given, its new state and note. False when it is not stored."""
        sets: dict[str, Any] = {"last_seen": self._clock()}
        if state is not None:
            sets["state"] = _check_in(state, KEEPER_STATES, "state")
        if note is not None:
            sets["note"] = note
        cols = ", ".join(f"{c} = ?" for c in sets)
        with self._write() as db:
            cur = db.execute(f"UPDATE keepers SET {cols} WHERE slug = ?",
                             (*sets.values(), slug))
            return cur.rowcount == 1

    def remove_keeper(self, slug: str) -> bool:
        with self._write() as db:
            return db.execute("DELETE FROM keepers WHERE slug = ?",
                              (slug,)).rowcount == 1

    def get_keeper(self, slug: str) -> Keeper | None:
        rows = self._read(f"SELECT {_KEEPER_COLS} FROM keepers "
                          "WHERE slug = ?", (slug,))
        return Keeper(*rows[0]) if rows else None

    def list_keepers(self, *, session: str | None = None,
                     states: Sequence[str] | None = None) -> list[Keeper]:
        where, args = _filters(("session_key", session), ("state", states))
        return [Keeper(*r) for r in self._read(
            f"SELECT {_KEEPER_COLS} FROM keepers{where} "
            "ORDER BY started_at, slug", args)]

    # -- launches -----------------------------------------------------

    def add_launch(self, launch_id: str, *, machine: str, harness: str,
                   cwd: str, options: dict | None = None,
                   command_id: str | None = None,
                   session: str | None = None) -> Launch:
        """Record a new launch, ``pending``. ``session`` is the session
        key when the adapter names its id up front. Raises
        StoreConflict when the launch id, or the command that asked
        for it, already has a launch."""
        _check_id(launch_id, "launch_id")
        _check_text(machine, "machine")
        _check_text(harness, "harness")
        _check_text(cwd, "cwd")
        if command_id is not None:
            _check_id(command_id, "command_id")
        if session is not None:
            _check_text(session, "session")
        opts = _options(options)
        with self._write() as db:
            try:
                db.execute(
                    "INSERT INTO launches(launch_id, command_id, "
                    "session_key, machine, harness, cwd, options, state, "
                    "created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (launch_id, command_id, session, machine, harness, cwd,
                     opts, self._clock()))
            except sqlite3.IntegrityError:
                raise StoreConflict(
                    f"launch {launch_id} (command {command_id}) is "
                    "already stored") from None
        found = self.get_launch(launch_id)
        assert found is not None
        return found

    def settle_launch(self, launch_id: str, state: str | None = None, *,
                      session: str | None = None,
                      note: str | None = None) -> bool:
        """Update a pending launch; False when it is unknown or already
        settled (a launch settles once, so a late failure cannot undo a
        start). Without ``state`` it only records progress."""
        sets: dict[str, Any] = {}
        if state is not None:
            if state == "pending":
                raise ValueError("settle_launch: state cannot go back to "
                                 "pending")
            sets["state"] = _check_in(state, LAUNCH_STATES, "state")
            sets["settled_at"] = self._clock()
        if session is not None:
            sets["session_key"] = _check_text(session, "session")
        if note is not None:
            sets["note"] = note
        if not sets:
            raise ValueError("settle_launch: nothing to change")
        cols = ", ".join(f"{c} = ?" for c in sets)
        with self._write() as db:
            cur = db.execute(
                f"UPDATE launches SET {cols} WHERE launch_id = ? "
                "AND state = 'pending'", (*sets.values(), launch_id))
            return cur.rowcount == 1

    def get_launch(self, launch_id: str) -> Launch | None:
        rows = self._read(f"SELECT {_LAUNCH_COLS} FROM launches "
                          "WHERE launch_id = ?", (launch_id,))
        return _launch(rows[0]) if rows else None

    def launch_for_command(self, command_id: str) -> Launch | None:
        """The launch a command asked for, so a retried
        ``POST /v1/sessions`` gets the same launch id back."""
        rows = self._read(f"SELECT {_LAUNCH_COLS} FROM launches "
                          "WHERE command_id = ?", (command_id,))
        return _launch(rows[0]) if rows else None

    def list_launches(self, *, states: Sequence[str] | None = None
                      ) -> list[Launch]:
        where, args = _filters(("state", states))
        return [_launch(r) for r in self._read(
            f"SELECT {_LAUNCH_COLS} FROM launches{where} "
            "ORDER BY created_at, launch_id", args)]

    # -- commands -----------------------------------------------------

    def record_command(
        self, command_id: str, *, session: str, op: str,
        status: str, payload: dict | None = None, machine: str = "",
        device: str | None = None, reason: str | None = None,
        hold_s: float = HOLD_S,
    ) -> Receipt:
        """Store a command and answer its receipt, atomically.

        The first call with ``command_id`` stores the command with
        ``status`` (``accepted``, ``queued``, ``rejected`` or ``held``)
        and answers it. Every later call answers ``duplicate`` with the
        stored receipt and changes nothing, so a retried send or
        approval is never applied twice: apply a command only when the
        answer is not a duplicate. A later call that reuses the id for
        a different command (another session, machine, op or payload)
        answers ``rejected`` with ``conflict=True`` and stores nothing.
        ``device`` is not compared: it is who sent it, not what it is.

        ``held`` commands expire ``hold_s`` after they arrive.
        ``rejected`` ones get a ``{ok: false}`` result at once, so they
        count as finished.
        """
        _check_id(command_id, "command_id")
        _check_text(session, "session", empty=True)
        _check_text(machine, "machine", empty=True)
        if not isinstance(op, str) or not _OP_RE.fullmatch(op):
            raise ValueError("op must be a short lower-case name")
        _check_in(status, STORED_STATUSES, "status")
        if device is not None and (not isinstance(device, str)
                                   or not _DEVICE_RE.fullmatch(device)):
            raise ValueError("device must be a device id")
        if not isinstance(hold_s, (int, float)) or hold_s <= 0:
            raise ValueError("hold_s must be a positive number")
        body = _payload(payload)
        now = self._clock()
        expires = now + hold_s if status == "held" else None
        result = finished = None
        if status == "rejected":
            result = _result({"ok": False, "note": reason}, op)
            finished = now
        with self._write() as db:
            row = db.execute(
                "SELECT session_key, machine, op, payload, status, reason, "
                "result FROM commands WHERE command_id = ?",
                (command_id,)).fetchone()
            if row is not None:
                if (row[0], row[1], row[2], row[3]) != (session, machine,
                                                        op, body):
                    return Receipt(
                        command_id, "rejected", op, conflict=True,
                        reason="command_id was already used for a "
                               "different command")
                return Receipt(
                    command_id, "duplicate", row[2], reason=row[5],
                    original=row[4], duplicate=True,
                    result=json.loads(row[6]) if row[6] else None)
            db.execute(
                "INSERT INTO commands(command_id, session_key, machine, op, "
                "payload, device, status, reason, result, received_at, "
                "updated_at, expires_at, finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (command_id, session, machine, op, body, device, status,
                 reason, result, now, now, expires, finished))
        return Receipt(command_id, status, op, reason=reason,
                       result=json.loads(result) if result else None)

    def transition(self, command_id: str, to: str, *,
                   reason: str | None = None,
                   result: dict | None = None) -> bool:
        """Move a command along: ``held`` to ``accepted``, ``queued`` or
        ``rejected``; ``queued`` to ``accepted`` or ``rejected``.
        Atomic and conditional: False when the command is unknown or
        its status does not allow the move, so two threads delivering
        the same held command cannot both win. A held command at or
        past its deadline cannot be delivered even before
        ``expire_held`` sweeps it: False, and the row is left for the
        sweep to reject (``held`` to ``rejected`` stays allowed).
        A move to ``rejected`` stores ``result`` (default ``{ok:
        false, note: reason}``) and finishes the command."""
        _check_in(to, STORED_STATUSES, "to")
        if result is not None and to != "rejected":
            raise ValueError("only a rejection carries a result here; "
                             "use set_result")
        now = self._clock()
        with self._write() as db:
            row = db.execute("SELECT status, op, expires_at FROM commands "
                             "WHERE command_id = ?", (command_id,)
                             ).fetchone()
            if row is None or to not in _TRANSITIONS[row[0]]:
                return False
            if row[0] == "held" and to != "rejected" \
                    and row[2] is not None and row[2] <= now:
                return False
            if to == "rejected":
                body = _result(result if result is not None
                               else {"ok": False, "note": reason}, row[1])
                db.execute(
                    "UPDATE commands SET status = 'rejected', reason = ?, "
                    "result = ?, expires_at = NULL, finished_at = ?, "
                    "updated_at = ? WHERE command_id = ?",
                    (reason, body, now, now, command_id))
            else:
                # Leaving ``held`` drops its reason ("machine asleep")
                # unless the caller gives a new one.
                db.execute(
                    "UPDATE commands SET status = ?, expires_at = NULL, "
                    "reason = ?, updated_at = ? WHERE command_id = ?",
                    (to, reason, now, command_id))
            return True

    def set_result(self, command_id: str, result: dict) -> bool:
        """Store what a command did (the ``command.result`` data:
        ``ok`` and optionally ``note``, ``degraded``, ``cancelled``,
        ``request_id``, ``launch_id``). Once only, and only for an
        ``accepted`` or ``queued`` command: False otherwise, and the
        first result stays (a replayed result changes nothing)."""
        with self._write() as db:
            row = db.execute("SELECT status, op, result FROM commands "
                             "WHERE command_id = ?", (command_id,)
                             ).fetchone()
            if row is None or row[2] is not None \
                    or row[0] not in ("accepted", "queued"):
                return False
            now = self._clock()
            db.execute(
                "UPDATE commands SET result = ?, finished_at = ?, "
                "updated_at = ? WHERE command_id = ?",
                (_result(result, row[1]), now, now, command_id))
            return True

    def get_command(self, command_id: str) -> Command | None:
        rows = self._read(f"SELECT {_COMMAND_COLS} FROM commands "
                          "WHERE command_id = ?", (command_id,))
        return _command(rows[0]) if rows else None

    def list_held(self, machine: str | None = None) -> list[Command]:
        """Held commands still in time, in the order they arrived, for
        delivery when ``machine`` (or any machine) comes back. Claim
        each with ``transition(id, "accepted")`` before sending it."""
        where, args = _filters(("machine", machine))
        where = (where + " AND" if where else " WHERE") + \
            " status = 'held' AND expires_at > ?"
        return [_command(r) for r in self._read(
            f"SELECT {_COMMAND_COLS} FROM commands{where} ORDER BY id",
            (*args, self._clock()))]

    def expire_held(self) -> list[Command]:
        """Reject every held command past its deadline (reason
        ``expired``) and return them, oldest first, so the hub can send
        each one's ``command.result``."""
        now = self._clock()
        with self._write() as db:
            rows = db.execute(
                f"SELECT {_COMMAND_COLS} FROM commands WHERE status = "
                "'held' AND expires_at <= ? ORDER BY id", (now,)).fetchall()
            for r in rows:
                # The row's own hold, not HOLD_S: record_command takes
                # a custom ``hold_s``.
                held_for = _span(r[12] - r[10])
                body = _result({"ok": False, "note": (
                    "expired: the machine did not come back within "
                    f"{held_for}")}, r[4])
                db.execute(
                    "UPDATE commands SET status = 'rejected', reason = "
                    "'expired', result = ?, finished_at = ?, updated_at = ?"
                    " WHERE id = ?", (body, now, now, r[0]))
            done = db.execute(
                f"SELECT {_COMMAND_COLS} FROM commands WHERE id IN "
                f"({', '.join('?' * len(rows))}) ORDER BY id",
                [r[0] for r in rows]).fetchall() if rows else []
        return [_command(r) for r in done]

    def unfinished(self, *, machine: str | None = None,
                   session: str | None = None) -> list[Command]:
        """``accepted`` and ``queued`` commands with no result yet,
        oldest first: after a hub restart, the ones to check against
        their keeper (the hub may have died before it delivered one)."""
        where, args = _filters(("machine", machine),
                               ("session_key", session))
        where = (where + " AND" if where else " WHERE") + \
            " result IS NULL AND status IN ('accepted', 'queued')"
        return [_command(r) for r in self._read(
            f"SELECT {_COMMAND_COLS} FROM commands{where} ORDER BY id",
            args)]

    # -- retention and backup -----------------------------------------

    def prune(self, older_than_s: float = RETAIN_S) -> dict[str, int]:
        """Delete what finished more than ``older_than_s`` ago: commands
        with a result (never a held one, never one still running),
        settled launches, and keepers that exited or were lost and not
        heard from since. Sessions are never pruned. Returns how many
        rows went from each table.

        ``older_than_s`` may not be shorter than the hold: a retry is
        recognised only while its row exists."""
        if older_than_s < HOLD_S:
            raise ValueError("retention is shorter than the 24 hour hold")
        cutoff = self._clock() - older_than_s
        with self._write() as db:
            commands = db.execute(
                "DELETE FROM commands WHERE result IS NOT NULL AND "
                "status != 'held' AND finished_at < ?", (cutoff,)).rowcount
            launches = db.execute(
                "DELETE FROM launches WHERE state != 'pending' AND "
                "settled_at < ?", (cutoff,)).rowcount
            keepers = db.execute(
                "DELETE FROM keepers WHERE state IN ('exited', 'lost') AND "
                "last_seen < ?", (cutoff,)).rowcount
        return {"commands": commands, "launches": launches,
                "keepers": keepers}

    def backup(self, dest: Path | str) -> Path:
        """Copy the store to ``dest`` through SQLite's backup API, which
        reads through this connection and so includes commits still in
        the ``-wal`` file. ``dest`` must not exist; it is written beside
        itself under a temporary name, made a single self-contained
        file (rollback journal, no ``-wal``), then renamed into place,
        mode 0600."""
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(f"{dest} exists; backup never "
                                  "overwrites")
        tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
        for leftover in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm"),
                         Path(f"{tmp}-journal")):
            leftover.unlink(missing_ok=True)
        _private_file(tmp)
        try:
            out = sqlite3.connect(str(tmp), isolation_level=None)
            try:
                with self._lock:
                    self._db().backup(out)
                out.execute("PRAGMA journal_mode = DELETE")
                check = out.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise StoreError(f"backup failed its check: {check}")
            finally:
                out.close()
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dest


class _Tx:
    """``with store._write() as db``: the store's lock plus one
    ``BEGIN IMMEDIATE`` transaction, committed on success and rolled
    back on any error."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        try:
            db = self.store._db()
            db.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.store._lock.release()
            raise
        return db

    def __exit__(self, exc_type, exc, tb) -> None:
        db = self.store._db()
        try:
            if exc_type is None:
                try:
                    db.execute("COMMIT")
                except BaseException:
                    # A failed COMMIT can leave the transaction open;
                    # end it so the next write can begin.
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
            elif db.in_transaction:
                db.execute("ROLLBACK")
        finally:
            self.store._lock.release()


# ---------------------------------------------------------------------
# Row helpers

_SESSION_COLS = ("key, harness, harness_session_id, machine, cwd, state, "
                 "control, control_reason, adapter, options, epoch, "
                 "last_seq, created_at, updated_at")
_KEEPER_COLS = ("slug, session_key, pid, socket_path, ipc_version, state, "
                "note, started_at, last_seen")
_LAUNCH_COLS = ("launch_id, command_id, session_key, machine, harness, cwd, "
                "options, state, note, created_at, settled_at")
_COMMAND_COLS = ("id, command_id, session_key, machine, op, payload, "
                 "device, status, reason, result, received_at, updated_at, "
                 "expires_at, finished_at")


def _session(r: tuple) -> Session:
    return Session(*r[:9], json.loads(r[9]), *r[10:])


def _launch(r: tuple) -> Launch:
    return Launch(*r[:6], json.loads(r[6]), *r[7:])


def _command(r: tuple) -> Command:
    return Command(*r[:5], json.loads(r[5]), *r[6:9],
                   json.loads(r[9]) if r[9] else None, *r[10:])


def _control(control: Any, reason: Any) -> tuple[str, str | None]:
    _check_in(control, CONTROL_MODES, "control")
    if control == "read_only":
        return control, _check_text(reason, "control_reason")
    if reason is not None:
        raise ValueError("control_reason is only for read_only")
    return control, None


def _span(seconds: float) -> str:
    """A hold length for a person: "24 hours", "90 minutes", "45 seconds"."""
    for size, unit in ((3600, "hour"), (60, "minute"), (1, "second")):
        if seconds >= size and seconds % size == 0:
            count = int(seconds // size)
            return f"{count} {unit}" + ("" if count == 1 else "s")
    return f"{seconds:g} seconds"


def _positive(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{what} must be a positive int")
    return value


def _filters(*pairs: tuple[str, Any]) -> tuple[str, list]:
    """`` WHERE a = ? AND b IN (?, ?)`` for the pairs whose value is not
    None (a list or tuple value means "any of")."""
    parts: list[str] = []
    args: list = []
    for column, value in pairs:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if not values:
                parts.append("0")
                continue
            parts.append(f"{column} IN ({', '.join('?' * len(values))})")
            args.extend(values)
        else:
            parts.append(f"{column} = ?")
            args.append(value)
    return (" WHERE " + " AND ".join(parts) if parts else ""), args

