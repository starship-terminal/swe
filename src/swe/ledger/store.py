"""The ledger's SQLite store (ports ``LedgerStore.swift``).

Rows keep token counts, never dollars, and outlive the logs they came
from: Claude Code prunes old transcripts, and the totals must not shrink
when it does. Prices are applied when a page asks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from swe.ledger.models import MAX_TOKENS, LedgerRecord
from swe.paths import data_dir_for

SCHEMA = 1
_TOTAL = "input + output + cache_read + cache_write + reasoning"


def ledger_path_for(home: Path | None = None) -> Path:
    """``~/.swe/data/web-ledger/ledger.sqlite``. Apart from the Mac
    app's ``data/ledger/`` on purpose: the two schemas differ."""
    return data_dir_for(home) / "web-ledger" / "ledger.sqlite"


class LedgerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS events (
                key TEXT PRIMARY KEY, msg TEXT, sidechain INTEGER NOT NULL DEFAULT 0,
                harness TEXT NOT NULL, account TEXT NOT NULL, session TEXT NOT NULL,
                cwd TEXT, model TEXT NOT NULL, tier TEXT, ts REAL NOT NULL,
                input INTEGER NOT NULL, output INTEGER NOT NULL,
                cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
                cache_write_1h INTEGER NOT NULL, reasoning INTEGER NOT NULL, cost REAL
            );
            CREATE INDEX IF NOT EXISTS events_msg ON events(msg) WHERE msg IS NOT NULL;
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, inode INTEGER, size INTEGER,
                mtime_ns INTEGER, offset INTEGER, fp TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', '{SCHEMA}');
        """)
        # Stores made before read cursors were fingerprinted, and before
        # session files borrowed the cursor's state slot for a watched
        # sibling's stamp.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(files)")}
        if "fp" not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN fp TEXT")
        if "state" not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN state TEXT")
        # Session-total events never count toward long-context surcharges.
        # Old rows are correct with the 0 default, so no schema bump.
        event_columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "session_total" not in event_columns:
            self.db.execute("ALTER TABLE events ADD COLUMN session_total INTEGER NOT NULL DEFAULT 0")
        # Every reader caps a count at MAX_TOKENS now, but the OpenCode and
        # Devin readers once saved corrupt counts uncapped, and a row near
        # int64 overflows every SUM over the table. Neither a rescan (the
        # source is unchanged) nor an upsert (the larger total wins) would
        # replace it, so such rows are clamped here, once per store.
        if self.meta("tokens_capped") is None:
            counts = ("input", "output", "cache_read", "cache_write",
                      "cache_write_1h", "reasoning")
            self.db.execute(
                "UPDATE events SET "
                + ", ".join(f"{c} = MIN(MAX({c}, 0), {MAX_TOKENS})" for c in counts)
                + " WHERE " + " OR ".join(f"{c} > {MAX_TOKENS} OR {c} < 0" for c in counts))
            self.set_meta("tokens_capped", "1")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- writes ------------------------------------------------------------

    def upsert(self, rec: LedgerRecord) -> bool:
        """Keep one copy per response. Returns true when a row was
        inserted or changed; a re-fetch of the same event under the same
        key is a no-op (``LedgerStore.upsert``).

        Same key: an authoritative record is its source's latest word and
        replaces whenever any stored figure differs; a non-sidechain copy
        otherwise beats a sidechain one, then the larger total wins and a
        tie keeps the first seen. A sidechain copy also collides on its
        message id alone, because a sub-agent transcript can replay a
        parent message under a new request id.
        """
        new_total = rec.event.total
        row = self.db.execute(
            f"SELECT sidechain, {_TOTAL} FROM events WHERE key = ?", (rec.key,)
        ).fetchone()
        if row is not None:
            if rec.authoritative:
                # The source's own figure always wins, but only a changed
                # figure is a write (``sameEvent``).
                if self._same_event(rec):
                    return False
                self._write(rec)
                return True
            if _wins(rec.sidechain, new_total, bool(row[0]), row[1]):
                self._write(rec)
                return True
            if rec.relabels_model:
                return self.db.execute(
                    "UPDATE events SET model = ? WHERE key = ? AND model != ?",
                    (rec.event.model, rec.key, rec.event.model)).rowcount > 0
            return False
        if rec.msg is not None:
            row = self.db.execute(
                f"SELECT key, sidechain, {_TOTAL} FROM events"
                " WHERE msg = ? AND (sidechain = 1 OR ?) LIMIT 1",
                (rec.msg, int(rec.sidechain)),
            ).fetchone()
            if row is not None:
                if _wins(rec.sidechain, new_total, bool(row[1]), row[2]):
                    self.db.execute("DELETE FROM events WHERE key = ?", (row[0],))
                    self._write(rec)
                    return True
                return False
        self._write(rec)
        if rec.authoritative and not rec.skips_lookalikes:
            self._delete_lookalikes(rec)
        return True

    def _same_event(self, rec: LedgerRecord) -> bool:
        """Whether the row under ``rec.key`` already stores the same
        figures and dimensions (``sameEvent``): only then is an
        authoritative re-read a no-op rather than a write."""
        e = rec.event
        row = self.db.execute(
            "SELECT sidechain, input, output, cache_read, cache_write, cache_write_1h,"
            " reasoning, session_total, cost, ts, harness, account, session, cwd, model,"
            " tier FROM events WHERE key = ?", (rec.key,)).fetchone()
        if row is None:
            return False
        cost_matches = e.cost is None if row[8] is None else e.cost == row[8]
        return (row[0] == int(rec.sidechain)
                and row[1] == e.input and row[2] == e.output and row[3] == e.cache_read
                and row[4] == e.cache_write and row[5] == e.cache_write_1h
                and row[6] == e.reasoning and row[7] == int(e.session_total)
                and cost_matches and row[9] == e.ts
                and row[10] == e.harness and row[11] == e.account
                and row[12] == e.session and (row[13] or "") == (e.cwd or "")
                and row[14] == e.model and (row[15] or "") == (e.tier or ""))

    def _delete_lookalikes(self, rec: LedgerRecord) -> None:
        """Drop the row an older key scheme left for this same event
        (``deleteSameEvent``/``selectSameEventAnyProfile``): the Cursor
        feed once keyed on the token counts it reported, so a correction
        keyed a new row, and the old one must go or it double-counts."""
        e = rec.event
        if rec.account_spans_profiles:
            # The counts-bearing key was this record's key plus
            # ":<input>:<output>"; rebuilding it from a candidate's own
            # counts reproduces the key only when the candidate is the
            # same account's same event — another account's lookalike
            # (same day bucket, model and instant) keeps its own account
            # in the string and survives. The account dim is deliberately
            # not matched: a sibling profile's stale row is the same event.
            rows = self.db.execute(
                "SELECT key, input, output FROM events WHERE ts = ? AND key != ?"
                " AND msg IS NULL AND harness = ? AND session = ?"
                " AND COALESCE(cwd, '') = ? AND model = ? AND COALESCE(tier, '') = ?",
                (e.ts, rec.key, e.harness, e.session, e.cwd or "", e.model,
                 e.tier or "")).fetchall()
            for cand_key, cand_in, cand_out in rows:
                if cand_key == f"{rec.key}:{cand_in}:{cand_out}":
                    self.db.execute("DELETE FROM events WHERE key = ?", (cand_key,))
        else:
            self.db.execute(
                "DELETE FROM events WHERE harness = ? AND account = ? AND session = ?"
                " AND COALESCE(cwd, '') = ? AND model = ? AND COALESCE(tier, '') = ?"
                " AND ts = ? AND key != ? AND msg IS NULL",
                (e.harness, e.account, e.session, e.cwd or "", e.model,
                 e.tier or "", e.ts, rec.key))

    def _write(self, rec: LedgerRecord) -> None:
        e = rec.event
        self.db.execute(
            "INSERT OR REPLACE INTO events (key, msg, sidechain, harness, account, session,"
            " cwd, model, tier, ts, input, output, cache_read, cache_write, cache_write_1h,"
            " reasoning, session_total, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.key, rec.msg, int(rec.sidechain), e.harness, e.account, e.session, e.cwd,
             e.model, e.tier, e.ts, e.input, e.output, e.cache_read, e.cache_write,
             e.cache_write_1h, e.reasoning, int(e.session_total), e.cost),
        )

    def delete(self, key: str) -> None:
        """Drop the event under ``key`` when there is one — used by
        session-total sources whose file now reports nothing, so stale
        spend clears (``LedgerStore.delete``)."""
        self.db.execute("DELETE FROM events WHERE key = ?", (key,))

    def delete_dropped(self, prefix: str, kept: set[int],
                       previous: set[int] | None = None) -> None:
        """Drop events keyed ``prefix`` plus a turn number that ``kept``
        no longer lists. ``previous`` is the turn numbers the last read
        of this file saved; only those are deleted, each by its key, so
        the lookup uses the ``events.key`` index. Without it (a cursor
        from before that list) the prefix is scanned once. The match is
        the prefix and then digits only, so a session id that starts
        with another's never loses its rows."""
        if previous is not None:
            for turn in previous - kept:
                self.delete(prefix + str(turn))
            return
        for (key,) in self.db.execute(
                "SELECT key FROM events WHERE key >= ? AND key < ?",
                (prefix, prefix + "\uffff")):
            turn = key[len(prefix):]
            if turn.isdigit() and int(turn) not in kept:
                self.delete(key)

    def file_cursor(self, path: str) -> tuple | None:
        """``(inode, size, mtime_ns, offset, fp, state)`` from the last
        read; ``fp`` fingerprints the bytes read so far (see scan.py) and
        ``state`` is a session file's watched-sibling stamp."""
        return self.db.execute(
            "SELECT inode, size, mtime_ns, offset, fp, state FROM files WHERE path = ?",
            (path,),
        ).fetchone()

    def set_file_cursor(self, path: str, inode: int, size: int, mtime_ns: int,
                        offset: int, fp: str | None = None, state: str | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files (path, inode, size, mtime_ns, offset, fp, state)"
            " VALUES (?,?,?,?,?,?,?)", (path, inode, size, mtime_ns, offset, fp, state),
        )

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    # -- reads -------------------------------------------------------------

    def groups(self, start: float | None, end: float | None,
               thresholds: dict[str, int]) -> list[tuple]:
        """Token sums for pricing, one row per (harness, model, tier,
        15-minute bucket, long context, logged cost).

        A request whose prompt passes its model's long-context threshold
        is billed whole at the long rates, so ``thresholds`` (model to
        tokens) splits those requests into their own groups. Buckets are
        15 minutes because every time zone offset is a multiple of that,
        so a bucket never straddles two local days.
        """
        self._load_thresholds(thresholds)
        where, args = [], []
        if start is not None:
            where.append("e.ts >= ?")
            args.append(start)
        if end is not None:
            where.append("e.ts < ?")
            args.append(end)
        sql = f"""
            SELECT e.harness, e.model, COALESCE(e.tier, ''), CAST(e.ts / 900 AS INTEGER),
                CASE WHEN t.n IS NOT NULL AND e.session_total = 0
                     AND e.input + e.cache_read + e.cache_write > t.n THEN 1 ELSE 0 END,
                e.cost IS NOT NULL,
                SUM(e.input), SUM(e.output), SUM(e.cache_read), SUM(e.cache_write),
                SUM(e.cache_write_1h), SUM(e.reasoning), SUM(COALESCE(e.cost, 0))
            FROM events e LEFT JOIN thresholds t ON t.model = e.model
            {"WHERE " + " AND ".join(where) if where else ""}
            GROUP BY 1, 2, 3, 4, 5, 6
        """
        return self.db.execute(sql, args).fetchall()

    def session_groups(self, thresholds: dict[str, int]) -> list[tuple]:
        """Per-session sums for pricing, over all history: one row per
        (harness, session, model, tier, long context, logged cost), with
        the event count and first and last timestamps."""
        self._load_thresholds(thresholds)
        return self.db.execute(f"""
            SELECT e.harness, e.session, e.model, COALESCE(e.tier, ''),
                CASE WHEN t.n IS NOT NULL AND e.session_total = 0
                     AND e.input + e.cache_read + e.cache_write > t.n THEN 1 ELSE 0 END,
                e.cost IS NOT NULL,
                SUM(e.input), SUM(e.output), SUM(e.cache_read), SUM(e.cache_write),
                SUM(e.cache_write_1h), SUM(e.reasoning), SUM(COALESCE(e.cost, 0)),
                COUNT(*), MIN(e.ts), MAX(e.ts)
            FROM events e LEFT JOIN thresholds t ON t.model = e.model
            GROUP BY 1, 2, 3, 4, 5, 6
        """).fetchall()

    def activity_session_stats(self, harnesses: list[str]) -> dict[str, dict]:
        """Whole-history totals per harness, then per ``(account,
        session)``, for the Calendar's file sources to judge an accidental
        session (``Ledger.filterTrivialActivitySessions``): the keys
        ``sessions.is_trivial`` reads. One pass for every harness asked."""
        out: dict[str, dict] = {h: {} for h in harnesses}
        rows = self.db.execute(
            "SELECT harness, account, session, COUNT(*),"
            " SUM(input + output + cache_read + cache_write + reasoning),"
            " SUM(output), SUM(reasoning), MIN(ts), MAX(ts)"
            f" FROM events WHERE harness IN ({','.join('?' * len(harnesses))})"
            " GROUP BY harness, account, session",
            list(harnesses),
        ).fetchall()
        for harness, account, session, n, tokens, outp, reas, first, last in rows:
            out[harness][(account, session)] = {
                "events": n, "tokens": tokens, "output": outp, "reasoning": reas,
                "first_ts": first, "last_ts": last}
        return out

    def session_folders(self) -> list[tuple]:
        """``(harness, session, cwd, events, last ts)`` per folder a
        session's events name, for picking the folder most of them used."""
        return self.db.execute(
            "SELECT harness, session, cwd, COUNT(*), MAX(ts) FROM events"
            " WHERE cwd IS NOT NULL GROUP BY 1, 2, 3"
        ).fetchall()

    def _load_thresholds(self, thresholds: dict[str, int]) -> None:
        # Filling the temp table is DML, so sqlite3 opens an implicit
        # transaction for it. Commit that here, or the read that follows
        # runs inside it and pins a WAL snapshot nothing ever releases:
        # once another connection (the feed round's) commits, this
        # connection's next write must upgrade a stale snapshot, which
        # SQLite refuses at once with "database is locked", every time.
        # A caller already inside its own transaction keeps it.
        ours = not self.db.in_transaction
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS thresholds (model TEXT PRIMARY KEY, n INTEGER)")
        self.db.execute("DELETE FROM thresholds")
        self.db.executemany("INSERT INTO thresholds VALUES (?, ?)", list(thresholds.items()))
        if ours:
            self.db.commit()

    def models(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT model FROM events")]

    def first_ts(self) -> float | None:
        row = self.db.execute("SELECT MIN(ts) FROM events").fetchone()
        return row[0] if row else None

    def activity_events(self, start: float, end: float,
                        harnesses: list[str] | None = None) -> list[tuple]:
        """Every response in ``[start, end)``, one session's in a row and
        oldest first, for the Calendar's activity blocks: ``(harness,
        account, session, cwd, model, tier, ts, input, output, cache_read,
        cache_write, cache_write_1h, reasoning, cost)``."""
        where, args = "ts >= ? AND ts < ?", [start, end]
        if harnesses is not None:
            where += f" AND harness IN ({','.join('?' * len(harnesses))})"
            args += list(harnesses)
        return self.db.execute(
            "SELECT harness, account, session, cwd, model, COALESCE(tier, ''), ts, input,"
            " output, cache_read, cache_write, cache_write_1h, reasoning, cost"
            f" FROM events WHERE {where} ORDER BY harness, account, session, ts, key",
            args,
        ).fetchall()


def _wins(new_sidechain: bool, new_total: int, old_sidechain: bool, old_total: int) -> bool:
    if new_sidechain != old_sidechain:
        return not new_sidechain
    return new_total > old_total
