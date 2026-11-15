"""ledger/store.py and scan.py: dedup rules and incremental reads."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from swe.ledger.models import LedgerRecord, UsageEvent
from swe.ledger.scan import scan
from swe.ledger.store import LedgerStore, ledger_path_for

from tests._tempdir import temp_dir


def _rec(key, total, msg=None, sidechain=False, ts=1000.0):
    ev = UsageEvent(harness="claude", account="/h", session="s", cwd=None,
                    model="m", ts=ts, input=total)
    return LedgerRecord(key=key, event=ev, msg=msg, sidechain=sidechain)


def _inputs(store):
    return dict(store.db.execute("SELECT key, input FROM events"))


class TestUpsert(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(temp_dir(self) / "l.sqlite")
        self.addCleanup(self.store.close)

    def test_larger_total_wins_and_tie_keeps_first(self):
        self.store.upsert(_rec("k", 5))
        self.store.upsert(_rec("k", 9))
        self.store.upsert(_rec("k", 3))
        self.assertEqual(_inputs(self.store), {"k": 9})
        first = _rec("k2", 4)
        first.event.session = "first"
        self.store.upsert(first)
        second = _rec("k2", 4)
        second.event.session = "second"
        self.store.upsert(second)
        row = self.store.db.execute("SELECT session FROM events WHERE key='k2'").fetchone()
        self.assertEqual(row[0], "first")

    def test_a_relabelling_record_renames_the_row_without_touching_its_figures(self):
        def reread(key, total, relabels):
            rec = _rec(key, total)
            rec.event.model = "p/m"
            rec.relabels_model = relabels
            self.store.upsert(rec)

        def row(key):
            return self.store.db.execute(
                "SELECT model, input FROM events WHERE key = ?", (key,)).fetchone()

        for key, first, again, expected in (
            ("tie", 5, 5, ("p/m", 5)), ("smaller", 5, 3, ("p/m", 5)), ("larger", 5, 9, ("p/m", 9)),
        ):
            self.store.upsert(_rec(key, first))
            self.assertEqual(row(key), ("m", first), key)
            reread(key, again, relabels=True)
            self.assertEqual(row(key), expected, key)
        # A plain re-read never renames a row.
        self.store.upsert(_rec("plain", 5))
        reread("plain", 5, relabels=False)
        self.assertEqual(row("plain"), ("m", 5))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)

    def test_main_copy_beats_sidechain_on_key_and_message_id(self):
        self.store.upsert(_rec("a", 100, msg="m1", sidechain=True))
        self.store.upsert(_rec("b", 10, msg="m1", sidechain=False))
        self.assertEqual(_inputs(self.store), {"b": 10})
        # A later sidechain replay of the same message never displaces it.
        self.store.upsert(_rec("c", 500, msg="m1", sidechain=True))
        self.assertEqual(_inputs(self.store), {"b": 10})

    def test_two_main_copies_with_different_request_ids_both_count(self):
        self.store.upsert(_rec("m1:r1", 10, msg="m1"))
        self.store.upsert(_rec("m1:r2", 20, msg="m1"))
        self.assertEqual(_inputs(self.store), {"m1:r1": 10, "m1:r2": 20})


def _line(mid, req, inp, ts="2026-10-03T10:00:00Z"):
    return json.dumps({
        "timestamp": ts, "sessionId": "s", "requestId": req,
        "message": {"id": mid, "model": "claude-opus-5",
                    "usage": {"input_tokens": inp, "output_tokens": 1}},
    }) + "\n"


class TestScan(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.proj = self.home / ".claude" / "projects" / "p"
        self.proj.mkdir(parents=True)
        self.store = LedgerStore(ledger_path_for(self.home))
        self.addCleanup(self.store.close)

    def test_appends_are_read_on_and_partial_lines_wait(self):
        f = self.proj / "a.jsonl"
        f.write_text(_line("m1", "r1", 5) + '{"partial')
        scan(self.store, self.home)
        self.assertEqual(_inputs(self.store), {"claude:m1:r1": 5})
        with open(f, "a") as fh:
            fh.write('": 1}\n' + _line("m2", "r2", 7))
        result = scan(self.store, self.home)
        self.assertEqual(result.files_read, 1)
        self.assertEqual(_inputs(self.store), {"claude:m1:r1": 5, "claude:m2:r2": 7})
        self.assertEqual(scan(self.store, self.home).files_read, 0)

    def test_a_line_nested_too_deep_to_parse_is_skipped(self):
        # json.loads raises RecursionError, not ValueError, on deep
        # nesting: the scan must skip that line, not stop at it on
        # every run with the cursor unmoved.
        deep = '{"usage": 1, "x": ' + "[" * 100_000 + "]" * 100_000 + "}\n"
        (self.proj / "a.jsonl").write_text(
            _line("m1", "r1", 5) + deep + _line("m2", "r2", 7))
        result = scan(self.store, self.home)
        self.assertEqual(result.errors, [])
        self.assertEqual(_inputs(self.store),
                         {"claude:m1:r1": 5, "claude:m2:r2": 7})

    def test_rows_outlive_deleted_logs(self):
        f = self.proj / "a.jsonl"
        f.write_text(_line("m1", "r1", 5))
        scan(self.store, self.home)
        f.unlink()
        scan(self.store, self.home)
        self.assertEqual(_inputs(self.store), {"claude:m1:r1": 5})

    def test_replaced_file_is_read_from_the_start(self):
        f = self.proj / "a.jsonl"
        f.write_text(_line("m1", "r1", 5) + _line("m2", "r2", 6))
        scan(self.store, self.home)
        f.unlink()
        f.write_text(_line("m3", "r3", 1))
        os.utime(f, (1, 1))
        scan(self.store, self.home)
        self.assertIn("claude:m3:r3", _inputs(self.store))

    def test_rewrite_in_place_is_read_again(self):
        f = self.proj / "a.jsonl"
        f.write_text(_line("m1", "r1", 5))
        scan(self.store, self.home)
        # Same inode and length, different bytes: a restore from backup.
        with open(f, "r+") as fh:
            fh.write(_line("m7", "r1", 5))
        os.utime(f, ns=(1, 1))
        scan(self.store, self.home)
        self.assertIn("claude:m7:r1", _inputs(self.store))

    @unittest.skipIf(os.geteuid() == 0, "root reads anything")
    def test_unreadable_file_is_retried_once_readable(self):
        f = self.proj / "a.jsonl"
        f.write_text(_line("m1", "r1", 5))
        f.chmod(0)
        result = scan(self.store, self.home)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(_inputs(self.store), {})
        f.chmod(0o644)
        scan(self.store, self.home)
        self.assertEqual(_inputs(self.store), {"claude:m1:r1": 5})

    def test_profile_homes_and_codex(self):
        prof = self.home / ".swe" / "accounts" / "claude" / "p1" / "home" / "projects" / "x"
        prof.mkdir(parents=True)
        (prof / "b.jsonl").write_text(_line("m9", "r9", 3))
        rollout = self.home / ".codex" / "sessions" / "2026" / "10" / "03"
        rollout.mkdir(parents=True)
        (rollout / "rollout-1.jsonl").write_text(json.dumps({
            "timestamp": "2026-10-03T10:00:00Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 4, "output_tokens": 2}}}}) + "\n")
        scan(self.store, self.home)
        keys = set(_inputs(self.store))
        self.assertIn("claude:m9:r9", keys)
        self.assertIn("codex:rollout-1:2026-10-03T10:00:00Z", keys)



class TestDatabaseSources(unittest.TestCase):
    """OpenCode and Devin read through scan(), against minimal schemas."""

    def setUp(self):
        self.home = temp_dir(self)
        self.store = LedgerStore(ledger_path_for(self.home))
        self.addCleanup(self.store.close)

    def _db(self, rel, script):
        import sqlite3
        path = self.home / rel
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript(script)
        conn.commit()
        conn.close()
        return path

    @staticmethod
    def _settle(path):
        """Age a database and its WAL a minute, as one nobody has written
        to lately: only such a stamp is trusted to skip a scan (#1930)."""
        for f in (path, Path(str(path) + "-wal")):
            if f.exists():
                old = f.stat().st_mtime_ns - 60_000_000_000
                os.utime(f, ns=(old, old))

    def _rows(self):
        return {r[0]: r[1:] for r in self.store.db.execute(
            "SELECT key, harness, model, input, output, reasoning, cache_read, cost, cwd FROM events")}

    def test_opencode(self):
        new = json.dumps({"role": "assistant", "modelID": "gpt-x", "cost": 0.5, "tokens": {
            "input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 0}, "total": 20}})
        # An older row folded reasoning into output (total excludes it).
        old = json.dumps({"role": "assistant", "modelID": "gpt-x", "cost": 0, "tokens": {
            "input": 1, "output": 5, "reasoning": 2, "cache": {"read": 0, "write": 0}, "total": 6}})
        user = json.dumps({"role": "user", "tokens": {"input": 9}})
        self._db(".local/share/opencode/opencode.db", f"""
            CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT);
            INSERT INTO session VALUES ('s1', '/w');
            INSERT INTO message VALUES ('m1', 's1', 1790000000000, 1, '{new}');
            INSERT INTO message VALUES ('m2', 's1', 1790000000000, 2, '{old}');
            INSERT INTO message VALUES ('m3', 's1', 1790000000000, 3, '{user}');
        """)
        self._settle(self.home / ".local/share/opencode/opencode.db")
        scan(self.store, self.home)
        rows = self._rows()
        self.assertEqual(rows["opencode:m1"], ("opencode", "gpt-x", 10, 5, 2, 3, 0.5, "/w"))
        self.assertEqual(rows["opencode:m2"][3:7], (3, 2, 0, None))
        self.assertNotIn("opencode:m3", rows)
        # Unchanged database: nothing read again.
        self.assertEqual(scan(self.store, self.home).files_read, 0)

    def test_a_home_whose_name_holds_uri_characters_still_reads(self):
        # The database opens as a file: URI; "#", "?" and "%20" in the
        # folder name must be encoded, not end the path or decode away.
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(
            suffix=" a#b?c%20d", ignore_cleanup_errors=True)))
        self.store = LedgerStore(ledger_path_for(self.home))
        self.addCleanup(self.store.close)
        reply = json.dumps({"role": "assistant", "modelID": "gpt-x", "tokens": {
            "input": 4, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
        self._db(".local/share/opencode/opencode.db", f"""
            CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT);
            INSERT INTO message VALUES ('m1', 's1', 1790000000000, 1, '{reply}');
        """)
        result = scan(self.store, self.home)
        self.assertEqual(result.errors, [])
        self.assertIn("opencode:m1", self._rows())

    def test_a_corrupt_huge_opencode_count_is_capped(self):
        # One count just under SQLite's int64 limit, one past it (stored
        # as REAL): uncapped, the first overflowed the Overview's SUMs and
        # the second failed the insert. Mac twin: #1312.
        from swe.ledger.models import MAX_TOKENS

        def reply(tokens):
            return json.dumps({"role": "assistant", "modelID": "gpt-x",
                               "tokens": {**tokens, "cache": {"read": 0, "write": 0}}})
        near = reply({"input": 9223372036854775000, "output": 9223372036854775000,
                      "reasoning": 1})
        past = reply({"input": 1e30, "output": "7", "reasoning": 0})
        self._db(".local/share/opencode/opencode.db", f"""
            CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT);
            INSERT INTO message VALUES ('m1', 's1', 1790000000000, 1, '{near}');
            INSERT INTO message VALUES ('m2', 's1', 1790000000000, 2, '{past}');
        """)
        result = scan(self.store, self.home)
        self.assertEqual(result.errors, [])
        rows = self._rows()
        self.assertEqual(rows["opencode:m1"][2:5], (MAX_TOKENS, MAX_TOKENS, 1))
        self.assertEqual(rows["opencode:m2"][2:4], (MAX_TOKENS, 7))
        self.assertEqual(
            self.store.db.execute("SELECT SUM(input) FROM events").fetchone()[0],
            2 * MAX_TOKENS)

    def test_a_huge_count_an_older_build_saved_is_clamped_once(self):
        # A row saved uncapped before the readers capped: an unchanged
        # source is never re-read and an upsert keeps the larger total,
        # so the store itself clamps such rows when it opens.
        from swe.ledger.models import MAX_TOKENS

        path = ledger_path_for(self.home)
        self.store.db.execute(
            "INSERT INTO events (key, harness, account, session, model, ts, input,"
            " output, cache_read, cache_write, cache_write_1h, reasoning)"
            " VALUES ('opencode:m1', 'opencode', '/h', 's', 'gpt-x', 1, ?, ?, 3, 0, 0, -1)",
            (9223372036854775000, 9223372036854775000))
        self.store.db.execute("DELETE FROM meta WHERE k = 'tokens_capped'")
        self.store.db.commit()
        self.store.close()
        self.store = LedgerStore(path)
        self.addCleanup(self.store.close)
        self.assertEqual(
            self.store.db.execute(
                "SELECT input, output, cache_read, reasoning FROM events").fetchone(),
            (MAX_TOKENS, MAX_TOKENS, 3, 0))
        self.assertEqual(
            self.store.db.execute("SELECT SUM(input + output) FROM events").fetchone()[0],
            2 * MAX_TOKENS)

    def test_a_corrupt_huge_devin_count_is_capped(self):
        from swe.ledger.models import MAX_TOKENS

        meta = {"request_id": "r1", "generation_model": "swe-2",
                "created_at": "2026-10-03T10:00:00Z",
                "metrics": {"input_tokens": 9223372036854775000,
                            "output_tokens": 9223372036854775000}}
        node = json.dumps({"role": "assistant", "metadata": meta})
        self._db(".local/share/devin/cli/sessions.db", f"""
            CREATE TABLE message_nodes (row_id INTEGER, session_id TEXT, created_at REAL, chat_message TEXT);
            INSERT INTO message_nodes VALUES (1, 'd1', 0, '{node}');
        """)
        scan(self.store, self.home)
        self.assertEqual(self._rows()["devin:r1"][2:4], (MAX_TOKENS, MAX_TOKENS))

    def test_devin(self):
        def node(rid, metrics=True):
            meta = {"request_id": rid, "generation_model": "swe-2",
                    "created_at": "2026-10-03T10:00:00Z"}
            if metrics:
                meta["metrics"] = {"input_tokens": 7, "output_tokens": 2,
                                   "cache_read_tokens": 4, "cache_creation_tokens": 1}
            return json.dumps({"role": "assistant", "metadata": meta})
        self._db(".local/share/devin/cli/sessions.db", f"""
            CREATE TABLE sessions (id TEXT, working_directory TEXT);
            CREATE TABLE message_nodes (row_id INTEGER, session_id TEXT, created_at REAL, chat_message TEXT);
            INSERT INTO sessions VALUES ('d1', '/repo');
            INSERT INTO message_nodes VALUES (1, 'd1', 0, '{node("r1")}');
            INSERT INTO message_nodes VALUES (2, 'd1', 0, '{node("r1")}');
            INSERT INTO message_nodes VALUES (3, 'd1', 0, '{node("r2", metrics=False)}');
            INSERT INTO message_nodes VALUES (4, 'd1', 0, 'not json');
        """)
        scan(self.store, self.home)
        rows = self._rows()
        self.assertEqual(set(rows), {"devin:r1"})
        self.assertEqual(rows["devin:r1"], ("devin", "swe-2", 7, 2, 0, 4, None, "/repo"))

    def test_wal_only_update_is_picked_up(self):
        import sqlite3

        def msg(i):
            return json.dumps({"role": "assistant", "modelID": "m", "tokens": {
                "input": i, "output": 1, "cache": {"read": 0, "write": 0}}})
        path = self._db(".local/share/opencode/opencode.db", f"""
            PRAGMA journal_mode=WAL;
            CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT);
            INSERT INTO message VALUES ('m1', 's', 1790000000000, 1, '{msg(1)}');
        """)
        scan(self.store, self.home)
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO message VALUES ('m2', 's', 1790000000000, 2, ?)", (msg(2),))
        writer.commit()
        scan(self.store, self.home)
        self.assertIn("opencode:m2", self._rows())

    def test_unreadable_database_is_a_note_not_a_crash(self):
        path = self.home / ".local/share/opencode/opencode.db"
        path.parent.mkdir(parents=True)
        path.write_text("not a database")
        result = scan(self.store, self.home)
        self.assertEqual(len(result.errors), 1)

    # -- watermarks (#1524): a changed database is read only past the
    # last row the previous scan saw, like DatabaseSources.swift.

    @staticmethod
    def _node(rid, inp=7, metrics=True):
        meta = {"request_id": rid, "generation_model": "swe-2",
                "created_at": "2026-10-03T10:00:00Z"}
        if metrics:
            meta["metrics"] = {"input_tokens": inp, "output_tokens": 2}
        return json.dumps({"role": "assistant", "metadata": meta})

    @staticmethod
    def _write(path, sql, *args):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _devin(self, *rows):
        path = self._db(".local/share/devin/cli/sessions.db", """
            CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT,
                created_at REAL, chat_message TEXT);""")
        for row_id, session, node in rows:
            self._write(path, "INSERT INTO message_nodes VALUES (?, ?, 0, ?)", row_id, session, node)
        return path

    def test_devin_reads_only_rows_past_its_watermark(self):
        path = self._devin((1, "d1", self._node("r1")), (2, "d1", self._node("r2")))
        self.assertEqual(scan(self.store, self.home).records, 2)
        self._write(path, "INSERT INTO message_nodes VALUES (3, 'd1', 0, ?)", self._node("r3"))
        self.assertEqual(scan(self.store, self.home).records, 1)
        self.assertEqual(set(self._rows()), {"devin:r1", "devin:r2", "devin:r3"})

    def test_a_devin_row_waiting_for_metrics_is_read_again_once_filled(self):
        path = self._devin((1, "d1", self._node("r1")), (2, "d1", self._node("r2", metrics=False)))
        scan(self.store, self.home)
        self.assertEqual(set(self._rows()), {"devin:r1"})
        # Devin fills the metrics in on the same row; another session moves on.
        self._write(path, "UPDATE message_nodes SET chat_message = ? WHERE row_id = 2",
                    self._node("r2", inp=11))
        self._write(path, "INSERT INTO message_nodes VALUES (3, 'd2', 0, ?)", self._node("r3"))
        self.assertEqual(scan(self.store, self.home).records, 2)  # rows 2 and 3, not 1
        self.assertEqual(self._rows()["devin:r2"][2], 11)

    def test_an_abandoned_devin_row_stops_holding_the_watermark(self):
        # A later row of the same session has metrics, so row 1 never will.
        path = self._devin((1, "d1", self._node("r1", metrics=False)), (2, "d1", self._node("r2")))
        scan(self.store, self.home)
        self._write(path, "INSERT INTO message_nodes VALUES (3, 'd1', 0, ?)", self._node("r3"))
        self.assertEqual(scan(self.store, self.home).records, 1)

    def test_a_write_that_keeps_the_scans_stamp_is_still_read(self):
        # A filesystem that stamps writes from a coarse clock (ext4 ticks
        # every few milliseconds, HFS+ every second) gives a write that
        # lands in the same tick as a scan the inode, size and mtime that
        # scan saw (#1930). utime puts that stamp back here.
        path = self._devin((1, "d1", self._node("r1")), (2, "d1", self._node("r2")))
        before = path.stat()
        self.assertEqual(scan(self.store, self.home).records, 2)
        self._write(path, "INSERT INTO message_nodes VALUES (3, 'd1', 0, ?)", self._node("r3"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))
        self.assertEqual(scan(self.store, self.home).records, 1)
        # Read again while unsettled, nothing is counted twice.
        scan(self.store, self.home)
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*), SUM(input) FROM events").fetchone(), (3, 21))

    def test_a_settled_database_is_skipped_until_it_moves(self):
        path = self._devin((1, "d1", self._node("r1")), (2, "d1", self._node("r2")))
        self._settle(path)
        self.assertEqual(scan(self.store, self.home).records, 2)
        self.assertEqual(scan(self.store, self.home).files_read, 0)
        self._write(path, "INSERT INTO message_nodes VALUES (3, 'd1', 0, ?)", self._node("r3"))
        self.assertEqual(scan(self.store, self.home).records, 1)
        self.assertIn("devin:r3", self._rows())

    def test_a_devin_database_rebuilt_in_place_is_read_from_the_start(self):
        path = self._devin(*[(i, "d1", self._node(f"r{i}")) for i in (1, 2, 3)])
        scan(self.store, self.home)
        inode = path.stat().st_ino
        self._write(path, "DELETE FROM message_nodes")
        self._write(path, "INSERT INTO message_nodes VALUES (1, 'd9', 0, ?)", self._node("r9"))
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(scan(self.store, self.home).records, 1)
        self.assertIn("devin:r9", self._rows())

    def test_a_replaced_devin_database_is_read_from_the_start(self):
        path = self._devin(*[(i, "d1", self._node(f"r{i}")) for i in (1, 2, 3)])
        scan(self.store, self.home)
        # A restored copy: a new file whose top row id is past the watermark.
        fresh = path.with_name("fresh.db")
        self._write(fresh, "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY,"
                           " session_id TEXT, created_at REAL, chat_message TEXT)")
        for i in (1, 2, 3, 4):
            self._write(fresh, "INSERT INTO message_nodes VALUES (?, 'd5', 0, ?)", i, self._node(f"n{i}"))
        os.replace(fresh, path)
        self.assertEqual(scan(self.store, self.home).records, 4)
        self.assertTrue({"devin:n1", "devin:n2", "devin:n3", "devin:n4"} <= set(self._rows()))

    def test_opencode_reads_only_rows_updated_since_its_watermark(self):
        def msg(i):
            return json.dumps({"role": "assistant", "modelID": "m", "tokens": {
                "input": i, "output": 1, "cache": {"read": 0, "write": 0}}})
        path = self._db(".local/share/opencode/opencode.db", f"""
            CREATE TABLE session (id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, time_updated INT, data TEXT);
            INSERT INTO message VALUES ('m1', 's', 1790000000000, 1, '{msg(1)}');
            INSERT INTO message VALUES ('m2', 's', 1790000000000, 2, '{msg(2)}');
        """)
        self.assertEqual(scan(self.store, self.home).records, 2)
        # m2 streams on (rewritten with a later time_updated); m3 is new.
        self._write(path, "UPDATE message SET time_updated = 5, data = ? WHERE id = 'm2'", msg(20))
        self._write(path, "INSERT INTO message VALUES ('m3', 's', 1790000000000, 6, ?)", msg(3))
        self.assertEqual(scan(self.store, self.home).records, 2)  # m2 and m3, not m1
        self.assertEqual(self._rows()["opencode:m2"][2], 20)
        self.assertIn("opencode:m3", self._rows())


class TestReadsLeaveNoTransaction(unittest.TestCase):
    """The pricing reads fill a temp table; that write must not leave
    the connection inside a transaction holding a read snapshot."""

    def test_groups_and_session_groups_end_their_transaction(self):
        path = temp_dir(self) / "l.sqlite"
        store = LedgerStore(path)
        self.addCleanup(store.close)
        store.upsert(_rec("k", 5))
        store.db.commit()
        store.groups(None, None, {"m": 1})
        self.assertFalse(store.db.in_transaction)
        store.session_groups({"m": 1})
        self.assertFalse(store.db.in_transaction)
        # Another connection commits; this one can still write.
        other = LedgerStore(path)
        other.set_meta("x", "1")
        other.db.commit()
        other.close()
        store.set_meta("y", "1")
        store.db.commit()


if __name__ == "__main__":
    unittest.main()
