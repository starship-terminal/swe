"""Per-session seq-numbered event journal — the reconnect story.

Every event the hub emits lands in ``data/remote/journal/<slug>.jsonl``
with a per-session ``seq``. A client that was offline for half an hour
rejoins its SSE stream with ``?last=<seq>`` and replays the gap instead of
refetching a transcript — the same thing Claude Remote Control's
server-side transcript buys them, except ours never leaves the tailnet.

The journal is a ring: appends past ``RING_MAX`` events or
``RING_MAX_BYTES`` rewrite the file keeping the newest ``RING_KEEP``
events, at most ``RING_KEEP_BYTES`` of them. Clients that fell off the back of the
ring get ``truncated: true`` in the replay header and refetch history
through ``/v1/sessions/{id}/messages``.
"""

from __future__ import annotations

import json
import os
import threading
import weakref
from collections.abc import Callable
from pathlib import Path

from swe.paths import remote_dir_for
from swe.remote.protocol import key_slug

# Ring bounds: keep journals small enough that replay stays a disk read,
# not a transcript-length scan.
RING_MAX = 4000
RING_KEEP = 2000
# The same ring by size: 4000 large tool events can run to hundreds of MB,
# and every replay and trim reads the whole file.
RING_MAX_BYTES = 16 << 20
RING_KEEP_BYTES = 8 << 20

# One lock per journal file, shared by every Journal on it: sessions
# append and replay side by side instead of queueing on one lock. RLock:
# append -> last_seq re-enters it. Held weakly: a lock lives while some
# caller holds it, so sessions the hub touched once don't pile up.
_LOCKS: "weakref.WeakValueDictionary[str, threading.RLock]" = \
    weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(str(path))
        if lock is None:
            lock = _LOCKS[str(path)] = threading.RLock()
        return lock


class Journal:
    """Append/read event envelopes per session key, seq-numbered."""

    def __init__(self, home: Path | str | None = None) -> None:
        root = remote_dir_for(Path(home) if home is not None else None)
        self.dir = root / "journal"
        # Every prompt, reply and tool call lands here: 0700, like the
        # harnesses' own transcript folders, so other local accounts
        # can't read sessions. A folder made before modes were set is
        # tightened in place.
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if self.dir.stat().st_mode & 0o077:
                os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self._seqs: dict[str, int] = {}
        # Line counts for journals already past the trim size, kept so a
        # trim check costs nothing per append. Counted once from disk.
        self._lines: dict[str, int] = {}
        # Byte sizes, read once from disk and kept up per append.
        self._bytes: dict[str, int] = {}

    def _file(self, session_key: str) -> Path:
        return self.dir / f"{key_slug(session_key)}.jsonl"

    def _load_seq(self, session_key: str) -> int:
        """Recover the last seq written for ``session_key``.

        Reads a tail window and walks it backward. A single event can
        exceed the window (full tool args run large), so the window
        doubles until a complete record parses or the file is spent —
        a >64 KiB final event must not reset numbering to 1.
        """
        path = self._file(session_key)
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        window = 65536
        while True:
            try:
                with open(path, "rb") as fh:
                    if size > window:
                        fh.seek(-window, os.SEEK_END)
                    tail = fh.read().decode("utf-8", errors="replace")
            except OSError:
                return 0
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                seq = rec.get("seq")
                if isinstance(seq, int) and seq > 0:
                    return seq
            if window >= size:
                return 0
            window = min(window * 4, size)

    def last_seq(self, session_key: str) -> int:
        with _lock_for(self._file(session_key)):
            if session_key not in self._seqs:
                self._seqs[session_key] = self._load_seq(session_key)
            return self._seqs[session_key]

    def append(self, session_key: str, event: dict) -> dict:
        """Stamp ``seq`` on ``event`` and append it; returns the event."""
        path = self._file(session_key)
        with _lock_for(path):
            seq = self.last_seq(session_key) + 1
            event = dict(event, seq=seq)
            self._seqs[session_key] = seq
            # Encoded once: the same bytes are written and counted.
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                             0o600)
                with os.fdopen(fd, "ab") as fh:
                    fh.write(line)
            except OSError:
                return event
            if str(path) in self._lines:
                self._lines[str(path)] += 1
            if str(path) in self._bytes:
                self._bytes[str(path)] += len(line)
            self._maybe_trim(path)
            return event

    def hold(self, session_key: str) -> threading.RLock:
        """The lock every append, read and trim of ``session_key``'s
        journal takes, for a caller that must decide and append as one
        step (``with journal.hold(key): ...``). Re-entrant, so the
        journal's own methods can be called under it."""
        return _lock_for(self._file(session_key))

    def append_at(self, session_key: str, event: dict) -> bool:
        """Append ``event`` under the seq it already carries, for a
        timeline numbered somewhere else (a session keeper's log).

        False, and nothing written, when the journal already holds that
        seq or a later one: a keeper's replay after a reconnect resends
        events, and each lands once. Seqs may skip ahead; they never go
        back. Raises ValueError for an event with no positive int seq.
        """
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError("append_at needs an event with a seq")
        path = self._file(session_key)
        with _lock_for(path):
            if seq <= self.last_seq(session_key):
                return False
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                             0o600)
                with os.fdopen(fd, "ab") as fh:
                    fh.write(line)
            except OSError:
                # Not written, so not counted: the keeper's next replay
                # brings it again.
                return False
            self._seqs[session_key] = seq
            if str(path) in self._lines:
                self._lines[str(path)] += 1
            if str(path) in self._bytes:
                self._bytes[str(path)] += len(line)
            self._maybe_trim(path)
            return True

    def drop(self, session_key: str) -> None:
        """Delete ``session_key``'s journal: its numbering starts over.
        For a timeline whose source was replaced (a keeper log that is
        not the one this journal followed)."""
        path = self._file(session_key)
        with _lock_for(path):
            try:
                path.unlink()
            except OSError:
                pass
            self._seqs.pop(session_key, None)
            self._lines.pop(str(path), None)
            self._bytes.pop(str(path), None)

    def _maybe_trim(self, path: Path) -> None:
        key = str(path)
        size = self._bytes.get(key)
        if size is None:
            try:
                size = self._bytes[key] = path.stat().st_size
            except OSError:
                return
        count = self._lines.get(key)
        if count is None and size <= RING_MAX_BYTES:
            # Small journals can't be past RING_MAX yet. Past 1 MB the
            # lines are counted once; appends keep the count after that.
            # (Re-reading the file per append made a journal of large
            # tool events, over 1 MB but under RING_MAX lines, cost a
            # full read on every event.)
            if size < 1024 * 1024:
                return
            count = self._count_lines(path)
            if count is None:
                return
            self._lines[key] = count
        if (count is None or count <= RING_MAX) and size <= RING_MAX_BYTES:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return
        kept = lines[-RING_KEEP:]
        kept_bytes = sum(len(line.encode("utf-8")) for line in kept)
        # By size too, oldest first; the newest event always stays.
        while len(kept) > 1 and kept_bytes > RING_KEEP_BYTES:
            kept_bytes -= len(kept.pop(0).encode("utf-8"))
        tmp = path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.replace(tmp, path)
            self._lines[key] = len(kept)
            self._bytes[key] = kept_bytes
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _count_lines(path: Path) -> int | None:
        try:
            with open(path, "rb") as fh:
                return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b""))
        except OSError:
            return None

    def sweep(self, cutoff: float, keep: Callable[[str], bool]) -> int:
        """Delete journals last appended to before ``cutoff``, except
        those whose file slug ``keep`` claims; returns how many.

        Each delete runs under the file's lock, so an append can't land
        in a file that is going away, and drops what this instance
        cached for it: a key appended to again starts over at seq 1, as
        it would after a restart.
        """
        try:
            files = list(self.dir.glob("*.jsonl"))
        except OSError:
            return 0
        removed = 0
        for path in files:
            if keep(path.stem):
                continue
            with _lock_for(path):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                except OSError:
                    continue
                self._lines.pop(str(path), None)
                self._bytes.pop(str(path), None)
                for key in [k for k in self._seqs
                            if key_slug(k) == path.stem]:
                    del self._seqs[key]
            removed += 1
        return removed

    def read_since(
        self,
        session_key: str,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> tuple[list[dict], bool]:
        """(events with seq > after_seq, truncated).

        ``truncated`` is True when the client asked for events the ring
        already dropped: the oldest seq on disk is past ``after_seq``.

        The read runs under the file's lock like appends and trims do —
        ``_maybe_trim`` rewrites the file under it, so a reader without
        it could answer a post-trim request from the pre-trim file and
        report ``truncated=False`` for events the ring already dropped.
        """
        path = self._file(session_key)
        with _lock_for(path):
            # Newest first, back to the first event at or before the
            # cursor: a reconnect near the head reads the last few
            # events, not the whole ring. Seqs only grow within a file.
            newest_first: list[dict] = []
            oldest = 0
            try:
                fh = open(path, "rb")
            except OSError:
                return [], False
            with fh:
                try:
                    if after_seq <= 0:
                        # A replay from the start reads forward, and
                        # stops at ``limit`` when it has one.
                        events = []
                        for raw in fh:
                            rec = _parse_line(raw)
                            if rec is None:
                                continue
                            events.append(rec)
                            if limit is not None and len(events) >= limit:
                                break
                        return events, False
                    for rec in _records_backward(fh):
                        oldest = rec["seq"]
                        if oldest <= after_seq:
                            break
                        newest_first.append(rec)
                except OSError:
                    return [], False
            events = newest_first[::-1]
            if limit is not None:
                events = events[:limit]
            truncated = bool(oldest and after_seq and after_seq < oldest - 1)
            return events, truncated

    def read_tail(
        self,
        session_key: str,
        limit: int,
        before_seq: int = 0,
        keep: Callable[[dict], dict | None] | None = None,
    ) -> list[dict]:
        """The newest ``limit`` events with seq < ``before_seq`` (any seq
        when 0), oldest first, reading the file backward from its end.

        ``keep`` maps each event to what is returned, or None to skip it;
        only kept events count toward ``limit``. A page of the newest
        messages costs a read of the last few of them, not a parse of
        the whole ring (up to ``RING_MAX_BYTES``). Under the file's lock
        for the reason ``read_since`` gives.
        """
        if limit <= 0:
            return []
        path = self._file(session_key)
        out: list[dict] = []
        with _lock_for(path):
            try:
                fh = open(path, "rb")
            except OSError:
                return []
            with fh:
                try:
                    for rec in _records_backward(fh):
                        if before_seq and rec["seq"] >= before_seq:
                            continue
                        picked = keep(rec) if keep is not None else rec
                        if picked is None:
                            continue
                        out.append(picked)
                        if len(out) >= limit:
                            break
                except OSError:
                    return []
        out.reverse()
        return out


# Bytes read per step of the backward walk.
_TAIL_CHUNK = 64 << 10


def _records_backward(fh):
    """The events of an open journal file, newest first.

    Reads ``_TAIL_CHUNK`` bytes at a time from the end, so a caller that
    stops early pays for the events it looked at, not the whole ring.
    Lines that are not an event with an int seq are skipped.
    """
    pos = fh.seek(0, os.SEEK_END)
    # The line the walk is inside, as the chunks read so far (later
    # bytes first). Joined once its start is found, so a large event is
    # copied once, not per chunk.
    parts: list[bytes] = []
    while pos > 0:
        step = min(_TAIL_CHUNK, pos)
        pos -= step
        fh.seek(pos)
        chunk = fh.read(step)
        if pos > 0 and b"\n" not in chunk:
            parts.append(chunk)
            continue
        lines = (chunk + b"".join(reversed(parts))).split(b"\n")
        # Until the read reaches the file's start, its first line may
        # begin in the next chunk back.
        parts = [lines.pop(0)] if pos > 0 else []
        for raw in reversed(lines):
            rec = _parse_line(raw)
            if rec is not None:
                yield rec


def _parse_line(raw: bytes) -> dict | None:
    """One journal line as an event with an int seq, or None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        rec = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("seq"), int):
        return None
    return rec
