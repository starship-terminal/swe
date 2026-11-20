"""The keeper's event log: one session's durable, seq-numbered timeline.

The log is the :class:`~swe.remote.adapters.base.EventSink` an adapter
emits into. It stamps each durable v2 event with the session's next
``seq``, appends it to a segment file, and hands it to live subscribers
(the keeper's IPC clients). ``message.delta`` is never written and gets
no ``seq``; it goes to subscribers only, and the log keeps the text of
each message in progress so a client that joins mid-reply can be given
it.

On disk, under ``data/remote/keepers/<slug>/log/``::

    events-000000000001.jsonl   one v2 envelope per line, seq ascending
    events-000000004711.jsonl   a segment is named by the first seq it may hold
    meta.json                   first_seq, the seq lease, the boot id, the
                                log.truncated marker
    lock                        flock'd by the one writer

Durability rules (``docs/specs/G4-keeper-event-log.md`` has the why):

- Every event is written to the OS (``os.write``, no user-space buffer)
  before it is delivered, so a keeper crash loses nothing a subscriber
  saw, and a replay from disk agrees with the live stream.
- ``SYNC_TYPES`` (accepted commands, approvals and input requests, turn
  boundaries, completed messages, checkpoints) are fsynced before they
  are delivered. Everything else is group-committed: fsynced at most
  ``commit_interval`` seconds after its write.
- ``seq`` is never reused, even when a power loss drops events that
  were already delivered: seqs are leased ahead in ``meta.json``, and a
  log reopened after an unclean shutdown on a different boot continues
  past the lease.
- A torn last line (a crash mid-write) is cut off on open. A corrupt
  line in the middle of a segment is left in place and skipped by every
  reader.

All clocks, the fsync call and the background flusher are injectable,
so tests never sleep.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from swe.paths import remote_dir_for
from swe.remote.adapters import events as ev
# Reused private helper (newest-first line walk); pinned by TestJournalHelper.
from swe.remote.journal import _records_backward
from swe.remote.protocol import key_slug

logger = logging.getLogger(__name__)

# Spec values (G1 section 2); every one is a constructor parameter.
SEGMENT_BYTES = 8 << 20
CAP_BYTES = 256 << 20
COMMIT_INTERVAL_S = 0.25
# Seqs leased ahead per meta.json write: the most a power loss can make
# the log skip, and one extra fsync per this many events.
SEQ_LEASE = 1024

# Durable-critical: fsynced before a subscriber sees them.
SYNC_TYPES = frozenset({
    "command.receipt",
    "approval.requested", "approval.resolved", "input.requested",
    "turn.started", "turn.ended",
    "message.completed",
    "checkpoint.created", "checkpoint.restored",
})

# The marker record kept in meta.json once old segments are dropped.
MARKER_TYPE = "log.truncated"

META_VERSION = 1
_META = "meta.json"
_LOCK = "lock"
_SEGMENT_RE = re.compile(r"^events-(\d+)\.jsonl$")
# session.state values that mean no turn is open and nothing is pending.
_AT_REST = frozenset({"idle", "agent_down", "parked", "closed"})


class LogError(Exception):
    """The log could not be opened."""


class LogLocked(LogError):
    """Another writer holds this log's lock."""


def log_dir_for(session_key: str, home: Path | str | None = None) -> Path:
    """``data/remote/keepers/<slug>/log`` for ``session_key``."""
    root = remote_dir_for(Path(home) if home is not None else None)
    return root / "keepers" / key_slug(session_key) / "log"


def segment_name(first_seq: int) -> str:
    """The file name of the segment whose first seq is ``first_seq``,
    zero-padded so a plain listing sorts in seq order."""
    return f"events-{first_seq:012d}.jsonl"


def full_fsync(fd: int) -> None:
    """Flush ``fd`` to stable storage. On macOS a plain fsync leaves the
    data in the drive's cache, so F_FULLFSYNC is tried first."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass
    os.fsync(fd)


def current_boot_id() -> str:
    """This boot's id, or "" when the OS will not say. A log reopened on
    the same boot after a crash lost nothing it had written (the page
    cache outlives the process); on a new boot it may have."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(None)
            name = b"kern.bootsessionuuid"
            size = ctypes.c_size_t(0)
            if libc.sysctlbyname(name, None, ctypes.byref(size), None, 0) == 0 \
                    and size.value:
                buf = ctypes.create_string_buffer(size.value)
                if libc.sysctlbyname(name, buf, ctypes.byref(size),
                                     None, 0) == 0:
                    return buf.value.decode("ascii", "replace")
        except (OSError, AttributeError, ValueError):
            pass
    return ""


# ---------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class Replay:
    """Events read back from the log.

    ``events`` are the records with ``after_seq < seq <= until_seq``,
    oldest first. ``oldest_seq`` is the first seq the log can still
    replay; ``truncated`` is true when the caller asked for history
    before it, and ``reason`` says why: ``evicted`` (the cap dropped it;
    ``marker`` is the ``log.truncated`` record) or ``before_start`` (it
    predates this log: a session continued from the hub journal).
    ``more`` is true when ``limit`` cut the read short; page on with
    ``after_seq`` set to the last event's seq. ``skipped`` counts corrupt
    lines passed over.
    """

    events: list
    oldest_seq: int
    truncated: bool = False
    reason: str = ""
    more: bool = False
    skipped: int = 0
    marker: dict | None = None


@dataclass(frozen=True)
class Recovery:
    """What opening the log found.

    ``last_seq`` is the newest seq on disk (0 for none); ``next_seq`` the
    one the next event gets. ``torn_bytes`` were cut from the tail (a
    crash mid-write); ``skipped_lines`` are corrupt lines left in place
    in the newest segment. ``seq_gap`` seqs were skipped because the
    last shutdown was unclean on another boot. ``open_turn`` and
    ``pending`` are the turn and the approval or input requests the log
    shows still open: G5 retires them when the agent is gone.
    """

    last_seq: int
    next_seq: int
    torn_bytes: int = 0
    skipped_lines: int = 0
    seq_gap: int = 0
    open_turn: str | None = None
    pending: tuple = ()


@dataclass
class Subscription:
    """A live subscriber, and the state it joined at.

    Every event with ``seq > last_seq`` (and every delta from now on)
    reaches the callback; replay the rest with
    ``log.replay(after, until_seq=sub.last_seq)``. ``live`` is the text so
    far of each message in progress (``id``, ``kind``, ``text``,
    ``turn_id?``, ``done?``), ``pending`` the open approval and input
    requests, ``open_turn`` the turn in flight.
    """

    last_seq: int
    live: list
    pending: list
    open_turn: str | None
    _close: Callable[[], None] = field(repr=False, default=lambda: None)

    def close(self) -> None:
        """Stop delivery to this subscriber. Idempotent."""
        self._close()


# ---------------------------------------------------------------------
# On-disk helpers


def _valid(rec: Any, floor: int = 1) -> bool:
    """Whether ``rec`` is a record a segment named ``floor`` may hold: a
    dict with a string type and an int seq of at least ``floor`` (and 1)."""
    if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
        return False
    seq = rec.get("seq")
    return isinstance(seq, int) and not isinstance(seq, bool) \
        and seq >= max(1, floor)


def _parse(raw: bytes, floor: int = 1) -> dict | None:
    """One complete log line as a record :func:`_valid` accepts for a
    segment named ``floor``, or None."""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if _valid(rec, floor) else None


def _list_segments(log_dir: Path) -> list[tuple[int, Path]]:
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    out = []
    for name in names:
        m = _SEGMENT_RE.match(name)
        if m:
            out.append((int(m.group(1)), log_dir / name))
    out.sort()
    return out


def _nat(value: Any) -> int:
    """``value`` if it is a non-negative int, else 0."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _read_meta(log_dir: Path) -> dict:
    try:
        meta = json.loads((log_dir / _META).read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict) or meta.get("v") != META_VERSION:
        return {}
    return meta


def _marker_of(meta: dict) -> dict | None:
    marker = meta.get("truncated")
    if isinstance(marker, dict) and _nat(marker.get("before_seq")):
        return marker
    return None


@dataclass
class _Scan:
    last: dict | None
    good_end: int
    size: int
    skipped: int


def _scan_segment(path: Path, floor: int) -> _Scan:
    """Read one segment forward. ``good_end`` is the byte just past the
    last valid, newline-terminated record; whatever follows it is a torn
    tail. Invalid lines before that point are counted in ``skipped``. A
    record is valid when it parses, its seq is at least the segment's
    name (``floor``) and above the record before it."""
    last = None
    good_end = offset = skipped = bad_run = 0
    with open(path, "rb") as fh:
        for raw in fh:
            offset += len(raw)
            if not raw.endswith(b"\n"):
                break
            rec = _parse(raw, floor)
            if rec is None or (
                    last is not None and rec["seq"] <= last["seq"]):
                bad_run += 1
                continue
            skipped += bad_run
            bad_run = 0
            last, good_end = rec, offset
        size = os.fstat(fh.fileno()).st_size
    return _Scan(last, good_end, size, skipped)


def _open_segments(segs: list[tuple[int, Path]], after_seq: int):
    """Open the segments a replay from ``after_seq`` reads: the last one
    named at or before ``after_seq + 1`` and every one after it. A file
    gone missing (dropped by the cap under a lock-free reader) takes the
    ones before it with it; returns the handles and the first seq past
    the loss (0 for none). When the newest listed file is the one gone,
    the writer has moved on to files this listing lacks, so the boundary
    is put past ``after_seq``: the replay reports truncation rather than
    an empty, complete answer."""
    start = 0
    for i, (first, _) in enumerate(segs):
        if first <= after_seq + 1:
            start = i
        else:
            break
    handles: list = []
    lost_before = 0
    for i in range(start, len(segs)):
        first, path = segs[i]
        try:
            handles.append((first, open(path, "rb")))
        except FileNotFoundError:
            for _, fh in handles:
                fh.close()
            handles = []
            if i + 1 < len(segs):
                lost_before = segs[i + 1][0]
            else:
                lost_before = max(first + 1, after_seq + 2)
    return handles, lost_before


def _replay(handles, after_seq: int, until_seq: int | None,
            limit: int | None, *, first_seq: int, marker: dict | None,
            lost_before: int = 0) -> Replay:
    before = _nat(marker.get("before_seq")) if marker else 0
    oldest = max(first_seq, before, lost_before)
    truncated = after_seq + 1 < oldest
    reason = ""
    if truncated:
        evicted = after_seq + 1 < max(before, lost_before)
        reason = "evicted" if evicted else "before_start"
    events: list = []
    skipped = 0
    more = False
    prev = after_seq
    done = False
    for first, fh in handles:
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # a write in progress, or a torn tail
            rec = _parse(raw, first)  # below its file's name: rejected
            if rec is None:
                skipped += 1
                continue
            seq = rec["seq"]
            if seq <= prev:
                continue  # before the cursor, or out of order
            if until_seq is not None and seq > until_seq:
                done = True
                break
            if limit is not None and len(events) >= limit:
                more = done = True
                break
            events.append(rec)
            prev = seq
        if done:
            break
    return Replay(events, oldest, truncated, reason, more, skipped,
                  dict(marker) if marker else None)


def read_log(session_key: str | None = None, after_seq: int = 0, *,
             home: Path | str | None = None, root: Path | str | None = None,
             until_seq: int | None = None,
             limit: int | None = None) -> Replay:
    """Replay a keeper log from disk without opening it for writing:
    for the hub once a keeper is gone, or a reader beside a live one. It
    takes no lock and changes nothing; a torn tail and corrupt lines are
    skipped, never repaired. ``root`` names the log folder directly."""
    if root is None and not session_key:
        raise ValueError("read_log needs a session_key or a root")
    log_dir = Path(root) if root is not None else log_dir_for(
        session_key, home)
    for attempt in (1, 2):
        segs = _list_segments(log_dir)
        # meta.json after the listing: the writer records the marker
        # before it unlinks a file, so a listed file that is gone by the
        # open is covered by this marker, and the retry lists afresh.
        meta = _read_meta(log_dir)
        marker = _marker_of(meta)
        before = _nat(marker.get("before_seq")) if marker else 0
        # Segments an interrupted eviction left behind are already gone.
        while len(segs) > 1 and segs[0][0] < before:
            segs.pop(0)
        first = _nat(meta.get("first_seq")) or (segs[0][0] if segs else 1)
        handles, lost = _open_segments(segs, after_seq)
        if not lost or attempt == 2:
            break
        for _, fh in handles:
            fh.close()
    try:
        return _replay(handles, after_seq, until_seq, limit, first_seq=first,
                       marker=marker, lost_before=lost)
    finally:
        for _, fh in handles:
            fh.close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _encode(event: dict) -> bytes:
    """One log line. UTF-8 as is; a lone surrogate (which validate's
    ASCII dump accepts) falls back to escaped ASCII."""
    try:
        text = json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False)
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(event, separators=(",", ":"), allow_nan=False)
        return (text + "\n").encode("ascii")


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------
# The log


class EventLog:
    """The durable event log of one session; the adapter's ``EventSink``.

    One writer per log: opening takes an exclusive, non-blocking flock
    on ``log/lock`` and raises :class:`LogLocked` if another
    ``EventLog`` (in any process) holds it. ``emit`` is thread-safe,
    never raises into the adapter and never calls back into it.

    ``first_seq`` is where a new log starts (the hub journal's next seq
    for a session continued in SWE); on an existing log it can only move
    the next seq forward. ``background=False`` turns off the flusher
    thread: the caller then drives group commit with :meth:`tick`.
    ``on_error(exc, event)`` hears about write and fsync failures.
    """

    def __init__(
        self,
        session_key: str,
        *,
        home: Path | str | None = None,
        root: Path | str | None = None,
        first_seq: int = 1,
        segment_bytes: int = SEGMENT_BYTES,
        cap_bytes: int = CAP_BYTES,
        commit_interval: float = COMMIT_INTERVAL_S,
        seq_lease: int = SEQ_LEASE,
        clock: Callable[[], float] = time.monotonic,
        fsync: Callable[[int], None] = full_fsync,
        boot_id: str | None = None,
        background: bool = True,
        on_error: Callable[[BaseException, dict | None], None] | None = None,
    ) -> None:
        if not session_key:
            raise ValueError("session_key is required")
        if first_seq < 1:
            raise ValueError("first_seq must be at least 1")
        if segment_bytes < 1 or seq_lease < 1 or commit_interval < 0:
            raise ValueError("sizes and intervals must be positive")
        if cap_bytes < segment_bytes:
            raise ValueError("cap_bytes must hold at least one segment")
        self.session_key = session_key
        self.segment_bytes = segment_bytes
        self.cap_bytes = cap_bytes
        self.commit_interval = commit_interval
        self.seq_lease = seq_lease
        self._clock = clock
        self._fsync = fsync
        self._boot = current_boot_id() if boot_id is None else boot_id
        self._on_error = on_error
        self.dir = Path(root) if root is not None else log_dir_for(
            session_key, home)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._delivering: int | None = None
        self._subs: list[tuple[object, Callable[[dict], None]]] = []
        # [first_seq, path, size] per segment, oldest first; the last
        # one is active.
        self._segments: list[list] = []
        self._total = 0
        self._fd: int | None = None
        self._needs_newline = False
        self._dirty_since: float | None = None
        self._next = first_seq
        self._reserved = 0
        self._first_seq = first_seq
        self._floor = 0
        self._marker: dict | None = None
        self._live: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        self._open_turn: str | None = None
        self._rejected_types: set[str] = set()
        self._stats = {"written": 0, "deltas": 0, "rejected": 0, "syncs": 0,
                       "errors": 0, "dropped": 0}

        self._lock_fd = self._take_lock()
        try:
            self.recovery = self._recover(first_seq)
        except BaseException:
            self._release()
            raise
        self._thread: threading.Thread | None = None
        if background:
            self._thread = threading.Thread(
                target=self._flusher, daemon=True,
                name=f"swe-keeper-log-{key_slug(session_key)}")
            self._thread.start()

    # -- opening -------------------------------------------------------

    def _take_lock(self) -> int:
        remote = self.dir.parent.parent.parent
        remote.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (self.dir.parent.parent, self.dir.parent, self.dir):
            _mkdir_private(path)
        fd = os.open(self.dir / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LogLocked(f"another writer holds {self.dir}") from exc
        return fd

    def _release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing drops the flock
            self._lock_fd = None

    def _recover(self, first_seq: int) -> Recovery:
        meta = _read_meta(self.dir)
        self._marker = _marker_of(meta)
        before = _nat(self._marker.get("before_seq")) if self._marker else 0
        segs = _list_segments(self.dir)
        # Finish an eviction a crash interrupted: the marker is written
        # before the old segments are unlinked.
        while len(segs) > 1 and segs[0][0] < before:
            segs.pop(0)[1].unlink(missing_ok=True)
        torn = skipped = 0
        last: dict | None = None
        active_size = 0
        while segs:
            first, path = segs[-1]
            scan = _scan_segment(path, first)
            skipped += scan.skipped
            if scan.size > scan.good_end:
                torn += scan.size - scan.good_end
                self._cut(path, scan.good_end)
            if scan.last is None:
                path.unlink(missing_ok=True)
                segs.pop()
                continue
            last, active_size = scan.last, scan.good_end
            break
        self._sync_dir()
        last_on_disk = last["seq"] if last else 0
        meta_first = _nat(meta.get("first_seq"))
        self._first_seq = meta_first or (segs[0][0] if segs else first_seq)
        # ``floor`` is the lowest seq any later open may hand out, and the
        # marker's ``before_seq`` was handed out before its segment went:
        # neither a crash gap nor an eviction can be undone by the disk.
        nxt = max(last_on_disk + 1, self._first_seq,
                  _nat(meta.get("floor")), before)
        gap = 0
        reserved = _nat(meta.get("reserved"))
        same_boot = bool(self._boot) and meta.get("boot") == self._boot
        if reserved >= nxt and not same_boot:
            # Unclean shutdown on another boot: events up to ``reserved``
            # may have been delivered and then lost with the page cache.
            gap = reserved + 1 - nxt
            nxt = reserved + 1
        nxt = max(nxt, first_seq)
        self._next = nxt
        for first, path in segs:
            size = active_size if path == segs[-1][1] else path.stat().st_size
            self._segments.append([first, path, size])
            self._total += size
        self._open_turn, pending = self._recover_state(segs)
        self._pending = {p["data"]["request_id"]: p for p in pending}
        self._reserved = nxt - 1 + self.seq_lease
        self._write_meta()
        if self._segments:
            self._fd = os.open(self._segments[-1][1],
                               os.O_WRONLY | os.O_APPEND)
            # What the last run wrote may still sit in the page cache;
            # make it durable before anything is appended after it.
            self._fsync(self._fd)
            try:
                self._evict()
            except OSError as exc:
                self._report(exc, None)
        return Recovery(last_on_disk, nxt, torn, skipped, gap,
                        self._open_turn, tuple(pending))

    def _cut(self, path: Path, size: int) -> None:
        fd = os.open(path, os.O_WRONLY)
        try:
            os.ftruncate(fd, size)
            self._fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _recover_state(segs) -> tuple[str | None, list[dict]]:
        """The open turn and open requests, read backward from the tail
        to the newest turn boundary (requests live inside a turn). Only
        records the forward scan would accept count: valid for their
        segment, and below the newer record already read."""
        resolved: set = set()
        pending: list[dict] = []
        newer: int | None = None
        for first, path in reversed(segs):
            with open(path, "rb") as fh:
                for rec in _records_backward(fh):
                    if not _valid(rec, first) or (
                            newer is not None and rec["seq"] >= newer):
                        continue
                    newer = rec["seq"]
                    etype = rec.get("type")
                    data = rec.get("data")
                    data = data if isinstance(data, dict) else {}
                    if etype == "turn.ended" or (
                            etype == "session.state"
                            and data.get("state") in _AT_REST):
                        return None, pending
                    if etype == "turn.started":
                        return rec.get("turn_id"), pending
                    rid = data.get("request_id")
                    if not isinstance(rid, str):
                        continue
                    if etype == "approval.resolved":
                        resolved.add(rid)
                    elif etype in ("approval.requested", "input.requested") \
                            and rid not in resolved:
                        pending.insert(0, rec)
        return None, pending

    # -- writing -------------------------------------------------------

    def emit(self, event: dict) -> dict | None:
        """Log and deliver one v2 event; returns it as stamped (deltas
        as given), or None when it was not logged.

        An event :func:`events.validate` rejects is dropped: not
        written, not delivered. The first rejected event of each type
        is recorded as an ``error`` event (``code: invalid_event``) so
        the timeline shows something went missing.
        """
        if self._delivering == threading.get_ident():
            raise RuntimeError("a subscriber must not emit into its log")
        problems = ev.validate(event)
        if problems:
            return self._reject(event, problems)
        if event["type"] in ev.TRANSIENT_TYPES:
            with self._lock:
                if self._closed:
                    self._stats["dropped"] += 1
                    return None
                self._stats["deltas"] += 1
                self._track(event)
                self._deliver(event)
            return event
        with self._lock:
            if self._closed:
                self._stats["dropped"] += 1
                return None
            seq = self._next
            stamped = dict(event)
            stamped["seq"] = seq
            lease = self._reserved
            try:
                if seq > self._reserved:
                    # The lease reaches disk before any seq past it.
                    self._reserved = seq - 1 + self.seq_lease
                    self._write_meta()
                self._append(_encode(stamped), seq)
            except OSError as exc:
                if self._reserved != lease:
                    self._reserved = lease  # retried on the next emit
                self._report(exc, stamped)
                return None
            self._next = seq + 1
            self._stats["written"] += 1
            if self._dirty_since is None:
                self._dirty_since = self._clock()
                self._cond.notify_all()
            if stamped["type"] in SYNC_TYPES or self._commit_due():
                try:
                    self._sync()
                except OSError as exc:
                    # Written but maybe not durable; it is on disk for
                    # replay, so it is delivered like the rest.
                    self._report(exc, stamped)
            self._track(stamped)
            self._deliver(stamped)
            return stamped

    def _reject(self, event: Any, problems: list[str]) -> None:
        etype = event.get("type") if isinstance(event, dict) else None
        key = etype[:64] if isinstance(etype, str) else "?"
        with self._lock:
            self._stats["rejected"] += 1
            first = key not in self._rejected_types
            self._rejected_types.add(key)
            closed = self._closed
        logger.warning("keeper log %s dropped an invalid %s event: %s",
                       self.dir, key, "; ".join(problems[:5]))
        if first and not closed:
            session = event.get("session") if isinstance(event, dict) else None
            if not isinstance(session, str) or not session:
                session = self.session_key
            message = (f"dropped an invalid {key} event: "
                       + "; ".join(problems[:5]))[:500]
            self.emit(ev.error(session, "invalid_event", message))
        return None

    def _append(self, line: bytes, seq: int) -> None:
        if self._fd is None or (
                self._segments[-1][2] > 0
                and self._segments[-1][2] + len(line) > self.segment_bytes):
            self._rotate(seq)
        active = self._segments[-1]
        if self._needs_newline:
            # A failed write left a partial line that could not be cut
            # off: end it, so it is one corrupt line and not glued to
            # the next record.
            _write_all(self._fd, b"\n")
            self._needs_newline = False
            self._grow(1)
        start = active[2]
        try:
            _write_all(self._fd, line)
        except OSError:
            try:
                os.ftruncate(self._fd, start)
            except OSError:
                self._needs_newline = True
                try:
                    self._grow(os.fstat(self._fd).st_size - start)
                except OSError:
                    pass
            raise
        self._grow(len(line))

    def _grow(self, n: int) -> None:
        self._segments[-1][2] += n
        self._total += n

    def _rotate(self, seq: int) -> None:
        if self._fd is not None:
            # Seal the full segment: only the newest one is repaired on
            # open, so an older one must be whole on disk.
            self._sync()
            os.close(self._fd)
            self._fd = None
        path = self.dir / segment_name(seq)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._fd = fd
        self._needs_newline = False
        size = os.fstat(fd).st_size
        self._segments.append([seq, path, size])
        self._total += size
        self._sync_dir()
        try:
            self._evict()
        except OSError as exc:
            self._report(exc, None)

    def _evict(self) -> None:
        """Drop the oldest sealed segments until the sealed ones plus a
        full active segment fit under the cap. The marker goes to
        meta.json (durably) before any file is unlinked."""
        sealed = self._total - self._segments[-1][2]
        n = 0
        while n < len(self._segments) - 1 \
                and sealed + self.segment_bytes > self.cap_bytes:
            sealed -= self._segments[n][2]
            n += 1
        if not n:
            return
        victims = self._segments[:n]
        dropped = sum(v[2] for v in victims)
        prev = self._marker or {}
        self._marker = {
            "type": MARKER_TYPE,
            "before_seq": self._segments[n][0],
            "ts": time.time(),
            "segments": _nat(prev.get("segments")) + n,
            "bytes": _nat(prev.get("bytes")) + dropped,
        }
        self._write_meta()
        for _, path, _ in victims:
            path.unlink(missing_ok=True)
        del self._segments[:n]
        self._total -= dropped
        self._sync_dir()

    def _write_meta(self) -> None:
        # Only ever rises: the next seq this run would hand out.
        self._floor = max(self._floor, self._next)
        meta = {"v": META_VERSION, "first_seq": self._first_seq,
                "floor": self._floor, "reserved": self._reserved,
                "boot": self._boot, "truncated": self._marker}
        body = (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8")
        tmp = self.dir / (_META + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, body)
            self._fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.dir / _META)
        self._sync_dir()

    def _sync_dir(self) -> None:
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            self._fsync(fd)
        finally:
            os.close(fd)

    # -- group commit --------------------------------------------------

    def _commit_due(self) -> bool:
        return self._dirty_since is not None and \
            self._clock() - self._dirty_since >= self.commit_interval

    def _sync(self) -> None:
        if self._fd is None or self._dirty_since is None:
            return
        self._fsync(self._fd)
        self._dirty_since = None
        self._stats["syncs"] += 1

    def tick(self) -> bool:
        """Group commit: fsync if the oldest unsynced write is
        ``commit_interval`` old. Returns whether it synced. The flusher
        thread calls this; with ``background=False`` the caller does."""
        with self._lock:
            if self._closed or not self._commit_due():
                return False
            try:
                self._sync()
            except OSError as exc:
                self._report(exc, None)
                return False
            return True

    def flush(self) -> None:
        """fsync everything written so far, now."""
        with self._lock:
            try:
                self._sync()
            except OSError as exc:
                self._report(exc, None)

    def _flusher(self) -> None:
        with self._cond:
            while not self._closed:
                if self._dirty_since is None:
                    self._cond.wait()
                    continue
                remaining = (self._dirty_since + self.commit_interval
                             - self._clock())
                if remaining > 0:
                    self._cond.wait(remaining)
                    continue
                try:
                    self._sync()
                except OSError as exc:
                    self._report(exc, None)
                    self._cond.wait(self.commit_interval or 0.25)

    def _report(self, exc: BaseException, event: dict | None) -> None:
        self._stats["errors"] += 1
        if self._on_error is not None:
            try:
                self._on_error(exc, event)
                return
            except Exception:
                logger.exception("keeper log on_error failed")
        logger.warning("keeper log %s: %s", self.dir, exc)

    # -- live state and subscribers -------------------------------------

    def _track(self, e: dict) -> None:
        etype = e["type"]
        data = e["data"]
        if etype == "message.delta":
            cur = self._live.get(data["id"])
            if cur is None:
                cur = self._live[data["id"]] = {
                    "id": data["id"], "kind": data["kind"], "text": ""}
                if e.get("turn_id"):
                    cur["turn_id"] = e["turn_id"]
            text = cur["text"]
            offset = data.get("offset")
            if offset is not None and offset <= len(text):
                text = text[:offset]
            cur["text"] = text + data["text"]
            if data.get("done"):
                cur["done"] = True
        elif etype in ("message.completed", "thinking.completed"):
            self._live.pop(data["id"], None)
        elif etype == "tool.started":
            self._live.pop(data["call_id"], None)
        elif etype == "turn.started":
            self._open_turn = e["turn_id"]
        elif etype == "turn.ended":
            self._live.clear()
            if self._open_turn == e["turn_id"]:
                self._open_turn = None
        elif etype in ("approval.requested", "input.requested"):
            self._pending[data["request_id"]] = e
        elif etype == "approval.resolved":
            self._pending.pop(data["request_id"], None)
        elif etype == "session.state" and data["state"] in _AT_REST:
            self._live.clear()
            self._open_turn = None

    def _deliver(self, e: dict) -> None:
        if not self._subs:
            return
        self._delivering = threading.get_ident()
        try:
            for token, callback in list(self._subs):
                try:
                    callback(e)
                except Exception:
                    logger.exception("keeper log subscriber failed; "
                                     "dropping it")
                    self._drop(token)
        finally:
            self._delivering = None

    def _drop(self, token: object) -> None:
        self._subs[:] = [s for s in self._subs if s[0] is not token]

    def subscribe(self, callback: Callable[[dict], None]) -> Subscription:
        """Deliver every later event to ``callback``, and say what state
        it joins at. Registration and the snapshot happen under the
        log's lock, so nothing falls between them.

        ``callback`` runs on the emitting thread with the log locked,
        in seq order: it must be quick (enqueue and return), must not
        block, and must not call ``emit`` or ``subscribe`` (both raise);
        closing its own subscription is fine. One that raises is dropped.
        Events are shared: treat them as read-only.
        """
        if self._delivering == threading.get_ident():
            raise RuntimeError("a subscriber must not subscribe from its "
                               "callback")
        token = object()

        def close() -> None:
            if self._delivering == threading.get_ident():
                # From a callback: this thread already holds the lock,
                # and delivery walks a copy of the list.
                self._drop(token)
                return
            with self._lock:
                self._drop(token)

        with self._lock:
            if self._closed:
                raise ValueError("the log is closed")
            self._subs.append((token, callback))
            return Subscription(
                last_seq=self._next - 1,
                live=[dict(m) for m in self._live.values()],
                pending=list(self._pending.values()),
                open_turn=self._open_turn,
                _close=close)

    # -- reading -------------------------------------------------------

    def replay(self, after_seq: int = 0, *, until_seq: int | None = None,
               limit: int | None = None) -> Replay:
        """The events with ``after_seq < seq <= until_seq`` (default: up
        to the last seq handed out when the call began), across
        segments, oldest first. The file handles are taken under the
        lock and read outside it, so a long replay never stalls emit."""
        with self._lock:
            last = self._next - 1
            until = last if until_seq is None else min(until_seq, last)
            segs = [(s[0], s[1]) for s in self._segments]
            first = self._first_seq
            marker = dict(self._marker) if self._marker else None
            handles, lost = _open_segments(segs, after_seq)
        try:
            return _replay(handles, after_seq, until, limit, first_seq=first,
                           marker=marker, lost_before=lost)
        finally:
            for _, fh in handles:
                fh.close()

    # -- state ---------------------------------------------------------

    @property
    def last_seq(self) -> int:
        """The last seq handed out (or skipped past after a crash)."""
        with self._lock:
            return self._next - 1

    @property
    def first_seq(self) -> int:
        """Where this log began."""
        return self._first_seq

    @property
    def oldest_seq(self) -> int:
        """The first seq a replay can still return."""
        with self._lock:
            before = _nat(self._marker.get("before_seq")) \
                if self._marker else 0
            return max(self._first_seq, before)

    @property
    def marker(self) -> dict | None:
        """The ``log.truncated`` record, once the cap has dropped
        anything."""
        with self._lock:
            return dict(self._marker) if self._marker else None

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict:
        """Counters: ``written``, ``deltas``, ``rejected``, ``syncs``
        (fsyncs of segment data), ``errors``, ``dropped`` (after close)."""
        with self._lock:
            out = dict(self._stats)
            out["segments"] = len(self._segments)
            out["bytes"] = self._total
            return out

    def segments(self) -> list[tuple[int, Path, int]]:
        """``(first_seq, path, size)`` per segment, oldest first."""
        with self._lock:
            return [tuple(s) for s in self._segments]

    # -- closing -------------------------------------------------------

    def close(self) -> None:
        """Flush, record a clean shutdown (the seq lease shrinks back to
        the last seq, so a reopen continues at ``last_seq + 1``), and
        release the lock. Idempotent; later emits are dropped."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._subs.clear()
            try:
                self._sync()
            except OSError as exc:
                self._report(exc, None)
            self._reserved = self._next - 1
            try:
                self._write_meta()
            except OSError as exc:
                self._report(exc, None)
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            self._release()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
