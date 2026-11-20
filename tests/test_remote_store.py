"""The hub's SQLite store (G6): schema, receipts, holds, migrations."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

from swe.paths import remote_dir_for
from swe.remote import store as store_mod
from swe.remote.adapters.events import RECEIPT_STATUSES
from swe.remote.adapters.fake import FakeClock
from swe.remote.store import (
    HOLD_S, SCHEMA_VERSION, Store, StoreConflict, StoreError, StoreNewer,
    command_id_of,
)
from tests._tempdir import temp_dir

SESSION = "claude:0b9e"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir(self)
        self.path = self.dir / "remote" / "remote.sqlite"
        self.clock = FakeClock()

    def open(self, path: Path | None = None) -> Store:
        s = Store(path or self.path, clock=self.clock)
        self.addCleanup(s.close)
        return s

    def raw(self, path: Path | None = None) -> sqlite3.Connection:
        conn = _raw(path or self.path)
        self.addCleanup(conn.close)
        return conn

    def siblings(self, path: Path | None = None) -> list[str]:
        p = path or self.path
        return sorted(n for n in os.listdir(p.parent)
                      if n.startswith(p.name))


class TestOpen(StoreCase):
    def test_fresh_store_is_wal_private_and_versioned(self) -> None:
        s = self.open()
        db = s._db()
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0],
                         "wal")
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0],
                         store_mod.BUSY_TIMEOUT_MS)
        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0],
                         store_mod.APPLICATION_ID)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(set(store_mod.TABLES) <= tables)
        self.assertEqual(_mode(self.path.parent), 0o700)
        s.record_command("c1", session=SESSION, op="send",
                         status="accepted", payload={"text": "hi"})
        # The WAL and shared-memory files take the database's mode.
        for name in ("", "-wal", "-shm"):
            self.assertEqual(_mode(Path(f"{self.path}{name}")), 0o600, name)

    def test_open_uses_the_remote_data_folder(self) -> None:
        home = self.dir / "home"
        with Store.open(home, clock=self.clock) as s:
            self.assertEqual(s.path, remote_dir_for(home) / "remote.sqlite")
            self.assertTrue(s.path.exists())
        self.assertEqual(store_mod.store_path(home), s.path)

    def test_reopen_keeps_rows_and_tightens_a_loose_mode(self) -> None:
        with Store(self.path, clock=self.clock) as s:
            s.record_command("c1", session=SESSION, op="send",
                             status="accepted")
        os.chmod(self.path, 0o644)
        s = self.open()
        self.assertEqual(_mode(self.path), 0o600)
        self.assertEqual(s.get_command("c1").status, "accepted")

    def test_close_is_idempotent_and_a_closed_store_refuses(self) -> None:
        s = Store(self.path, clock=self.clock)
        s.close()
        s.close()
        with self.assertRaises(StoreError):
            s.get_command("c1")
        with self.assertRaises(StoreError):
            s.record_command("c1", session=SESSION, op="send",
                             status="accepted")

    def test_newer_version_is_refused_and_left_byte_identical(self) -> None:
        with Store(self.path, clock=self.clock) as s:
            s.record_command("c1", session=SESSION, op="send",
                             status="accepted")
        conn = _raw(self.path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        before = self.path.read_bytes()
        names = self.siblings()
        with self.assertRaises(StoreNewer) as caught:
            Store(self.path, clock=self.clock)
        self.assertIn("99", str(caught.exception))
        self.assertIn("update swe", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        # Not even an empty -wal or -shm appears beside it.
        self.assertEqual(self.siblings(), names)

    def test_newer_version_still_in_the_wal_is_refused(self) -> None:
        with Store(self.path, clock=self.clock):
            pass
        # A newer swe bumped the version; the commit is in its -wal and
        # not yet in the main file's header.
        newer = self.raw()
        newer.execute("PRAGMA wal_autocheckpoint = 0")
        newer.execute("PRAGMA user_version = 7")
        before = self.path.read_bytes()
        self.assertEqual(int.from_bytes(before[60:64], "big"), 1)
        with self.assertRaises(StoreNewer):
            Store(self.path, clock=self.clock)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_foreign_or_broken_file_is_refused_untouched(self) -> None:
        self.path.parent.mkdir(parents=True)
        other = _raw(self.path)
        other.execute("CREATE TABLE t(x)")
        other.close()
        cases = {
            "foreign": self.path,
            "garbage": self.dir / "garbage.sqlite",
            "short": self.dir / "short.sqlite",
            "versioned": self.dir / "versioned.sqlite",
            "corrupt": self.dir / "corrupt.sqlite",
        }
        # A good-looking header over pages SQLite cannot read.
        cases["corrupt"].write_bytes(
            b"SQLite format 3\x00" + b"\x00" * 84 + b"\xff" * 4096)
        cases["garbage"].write_bytes(b"not a database at all " * 10)
        cases["short"].write_bytes(b"SQLite format 3\x00")
        conn = _raw(cases["versioned"])
        conn.execute("PRAGMA user_version = 1")  # but no application_id
        conn.close()
        for name, path in cases.items():
            before = path.read_bytes()
            with self.subTest(name), self.assertRaises(StoreError) as caught:
                Store(path, clock=self.clock)
            self.assertNotIsInstance(caught.exception, StoreNewer)
            self.assertIn("left as it is", str(caught.exception))
            self.assertEqual(path.read_bytes(), before)

    def test_unreadable_file_is_a_store_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"")
        with mock.patch("builtins.open", side_effect=PermissionError("no")):
            with self.assertRaises(StoreError):
                Store(self.path, clock=self.clock)

    def test_probe_failures_are_store_errors(self) -> None:
        with mock.patch.object(store_mod.sqlite3, "connect",
                               side_effect=sqlite3.OperationalError("x")):
            with self.assertRaises(StoreError):
                Store(self.path, clock=self.clock)

    def test_no_wal_is_refused(self) -> None:
        real = sqlite3.connect

        class NoWal:
            def __init__(self, *a, **kw) -> None:
                self.conn = real(*a, **kw)

            def execute(self, sql, *args):
                if "journal_mode" in sql:
                    return self.conn.execute("SELECT 'delete'")
                return self.conn.execute(sql, *args)

            def close(self) -> None:
                self.conn.close()

        def connect(*a, **kw):
            return NoWal(*a, **kw) if not kw.get("uri") else real(*a, **kw)

        with mock.patch.object(store_mod.sqlite3, "connect", connect):
            with self.assertRaises(StoreError) as caught:
                Store(self.path, clock=self.clock)
        self.assertIn("local disk", str(caught.exception))

    def test_two_first_opens_at_once_both_succeed(self) -> None:
        errors: list[BaseException] = []
        stores: list[Store] = []
        gate = threading.Barrier(4)

        def run() -> None:
            try:
                gate.wait()
                stores.append(Store(self.path, clock=self.clock))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for s in stores:
            self.addCleanup(s.close)
        self.assertEqual(errors, [])
        self.assertEqual(len(stores), 4)
        self.assertEqual(
            self.raw().execute("PRAGMA user_version").fetchone()[0],
            SCHEMA_VERSION)


def _v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")


def _v2_fails(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE half_done(x)")
    raise RuntimeError("step 2 broke")


class TestMigrations(StoreCase):
    def make_v1(self) -> None:
        """A version-1 file built by hand, as an older swe left it."""
        self.path.parent.mkdir(parents=True)
        conn = _raw(self.path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN")
        store_mod._v1(conn)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO sessions(key, harness, harness_session_id, machine,"
            " state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (SESSION, "claude", "0b9e", "m1", "idle", 1.0, 1.0))
        conn.execute(
            "INSERT INTO commands(command_id, session_key, op, payload, "
            "status, received_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            ("c1", SESSION, "send", "{}", "accepted", 1.0, 1.0))
        conn.execute("COMMIT")
        conn.close()

    def test_older_schema_migrates_in_order_and_keeps_rows(self) -> None:
        self.make_v1()
        steps = ((1, store_mod._v1), (2, _v2))
        with mock.patch.object(store_mod, "MIGRATIONS", steps):
            s = self.open()
            self.assertEqual(
                s._db().execute("PRAGMA user_version").fetchone()[0], 2)
        cols = {r[1] for r in self.raw().execute(
            "PRAGMA table_info(sessions)")}
        self.assertIn("title", cols)
        self.assertEqual(s.get_session(SESSION).state, "idle")
        self.assertEqual(s.get_command("c1").status, "accepted")

    def test_a_failed_step_leaves_the_old_version_and_tables(self) -> None:
        self.make_v1()
        steps = ((1, store_mod._v1), (2, _v2_fails))
        with mock.patch.object(store_mod, "MIGRATIONS", steps):
            with self.assertRaises(RuntimeError):
                Store(self.path, clock=self.clock)
        conn = self.raw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertIsNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'half_done'"
        ).fetchone())
        # The real v1 code still opens it.
        self.assertEqual(self.open().get_command("c1").status, "accepted")

    def test_a_failed_first_step_leaves_an_empty_file(self) -> None:
        def broken(conn: sqlite3.Connection) -> None:
            store_mod._v1(conn)
            raise RuntimeError("v1 broke")

        with mock.patch.object(store_mod, "MIGRATIONS", ((1, broken),)):
            with self.assertRaises(RuntimeError):
                Store(self.path, clock=self.clock)
        conn = self.raw()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0],
                         0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        conn.close()
        self.open()  # and the real code creates it afterwards

    def test_out_of_order_steps_are_refused(self) -> None:
        self.make_v1()
        steps = ((1, store_mod._v1), (3, _v2))
        with mock.patch.object(store_mod, "MIGRATIONS", steps):
            with self.assertRaises(StoreError) as caught:
                Store(self.path, clock=self.clock)
        self.assertIn("out of order", str(caught.exception))

    def test_version_bumped_between_probe_and_lock_is_refused(self) -> None:
        self.make_v1()
        steps = ((1, store_mod._v1), (2, _v2))
        real = store_mod._probe

        def probe_then_bump(path: Path):
            seen = real(path)
            conn = _raw(path)
            conn.execute("PRAGMA user_version = 5")
            conn.close()
            return seen

        with mock.patch.object(store_mod, "MIGRATIONS", steps), \
                mock.patch.object(store_mod, "_probe", probe_then_bump):
            with self.assertRaises(StoreNewer):
                Store(self.path, clock=self.clock)


class TestSessions(StoreCase):
    def put(self, s: Store, key: str = SESSION, **kw) -> store_mod.Session:
        fields = {"harness": "claude", "harness_session_id": key.split(":")[1],
                  "machine": "m1", "cwd": "/w", "adapter": "claude_stream"}
        fields.update(kw)
        return s.put_session(key, **fields)

    def test_round_trip_and_upsert_keeps_created_at(self) -> None:
        s = self.open()
        first = self.put(s, options={"model": "opus", "yolo": True})
        self.assertEqual(first.state, "starting")
        self.assertEqual(first.control, "full")
        self.assertIsNone(first.control_reason)
        self.assertEqual(first.options, {"model": "opus", "yolo": True})
        self.assertEqual((first.epoch, first.last_seq), (0, 0))
        self.clock.advance(5)
        again = self.put(s, state="idle", cwd="/w2")
        self.assertEqual(again.created_at, first.created_at)
        self.assertEqual(again.updated_at, first.created_at + 5)
        self.assertEqual((again.state, again.cwd), ("idle", "/w2"))
        self.assertEqual(again.options, {})

    def test_update_fields_and_control_rules(self) -> None:
        s = self.open()
        self.put(s)
        self.assertTrue(s.update_session(
            SESSION, state="running", epoch=2, last_seq=40,
            adapter=None, options={"effort": "high"}, cwd="/x"))
        row = s.get_session(SESSION)
        self.assertEqual((row.state, row.epoch, row.last_seq, row.adapter,
                          row.options, row.cwd),
                         ("running", 2, 40, None, {"effort": "high"}, "/x"))
        self.assertTrue(s.update_session(
            SESSION, control="read_only", control_reason="no adapter"))
        row = s.get_session(SESSION)
        self.assertEqual((row.control, row.control_reason),
                         ("read_only", "no adapter"))
        self.assertTrue(s.update_session(SESSION, control="full"))
        self.assertIsNone(s.get_session(SESSION).control_reason)
        self.assertTrue(s.update_session(SESSION))
        self.assertFalse(s.update_session("claude:nope"))
        self.assertFalse(s.update_session("claude:nope", state="idle"))
        bad = [
            {"control": "read_only"},
            {"control": "full", "control_reason": "why"},
            {"control_reason": "alone"},
            {"control": "sometimes"},
            {"state": "dreaming"},
            {"epoch": -1},
            {"last_seq": True},
            {"title": "x"},
            {"adapter": ""},
            {"options": {"api_key": "sk-1"}},
            {"options": {"model": 3}},
            {"options": ["model"]},
        ]
        for fields in bad:
            with self.subTest(fields), self.assertRaises(ValueError):
                s.update_session(SESSION, **fields)

    def test_note_seq_only_moves_forward(self) -> None:
        s = self.open()
        self.put(s)
        self.assertTrue(s.note_seq(SESSION, 10))
        self.assertTrue(s.note_seq(SESSION, 4))
        self.assertEqual(s.get_session(SESSION).last_seq, 10)
        self.assertFalse(s.note_seq("claude:nope", 1))
        with self.assertRaises(ValueError):
            s.note_seq(SESSION, -1)

    def test_list_filters(self) -> None:
        s = self.open()
        self.put(s, "claude:a", state="idle")
        self.clock.advance(1)
        self.put(s, "claude:b", state="running", machine="m2")
        self.clock.advance(1)
        self.put(s, "claude:c", state="closed")
        self.assertEqual([r.key for r in s.list_sessions()],
                         ["claude:a", "claude:b", "claude:c"])
        self.assertEqual([r.key for r in s.list_sessions(machine="m2")],
                         ["claude:b"])
        self.assertEqual(
            [r.key for r in s.list_sessions(states=["idle", "running"])],
            ["claude:a", "claude:b"])
        self.assertEqual(s.list_sessions(states=[]), [])

    def test_one_harness_session_has_one_key(self) -> None:
        s = self.open()
        self.put(s)
        with self.assertRaises(StoreConflict):
            self.put(s, "claude:other", harness_session_id="0b9e")
        self.assertIsNone(s.get_session("claude:other"))

    def test_bad_values_are_refused(self) -> None:
        s = self.open()
        for fields in ({"state": "nope"}, {"machine": ""},
                       {"control": "read_only"}, {"cwd": "a\x00b"},
                       {"options": {"token": "x"}}):
            with self.subTest(fields), self.assertRaises(ValueError):
                self.put(s, **fields)


class TestKeepers(StoreCase):
    def test_round_trip_touch_replace_and_remove(self) -> None:
        s = self.open()
        k = s.put_keeper("ab12", session=SESSION, pid=4242,
                         socket_path="/tmp/swe-501/ab12.sock", ipc_version=1)
        self.assertEqual((k.slug, k.session, k.pid, k.state, k.ipc_version),
                         ("ab12", SESSION, 4242, "starting", 1))
        self.clock.advance(3)
        self.assertTrue(s.touch_keeper("ab12", state="live", note="ok"))
        k = s.get_keeper("ab12")
        self.assertEqual((k.state, k.note, k.last_seen - k.started_at),
                         ("live", "ok", 3))
        self.assertTrue(s.touch_keeper("ab12"))
        self.assertFalse(s.touch_keeper("zz"))
        self.clock.advance(10)
        k2 = s.put_keeper("ab12", session=SESSION, pid=5000,
                          socket_path="/tmp/swe-501/ab12.sock",
                          ipc_version=2)
        self.assertEqual((k2.pid, k2.ipc_version, k2.state),
                         (5000, 2, "starting"))
        self.assertEqual(k2.started_at, self.clock())
        s.put_keeper("cd34", session="claude:z", pid=7,
                     socket_path="/tmp/swe-501/cd34.sock", ipc_version=1,
                     state="live")
        # Same start time: ties break by slug.
        self.assertEqual([x.slug for x in s.list_keepers()],
                         ["ab12", "cd34"])
        self.assertEqual([x.slug for x in s.list_keepers(session=SESSION)],
                         ["ab12"])
        self.assertEqual([x.slug for x in s.list_keepers(states=["live"])],
                         ["cd34"])
        self.assertTrue(s.remove_keeper("ab12"))
        self.assertFalse(s.remove_keeper("ab12"))
        self.assertIsNone(s.get_keeper("ab12"))

    def test_bad_values_are_refused(self) -> None:
        s = self.open()
        good = {"session": SESSION, "pid": 1, "socket_path": "/s",
                "ipc_version": 1}
        for change in ({"pid": 0}, {"pid": True}, {"ipc_version": 0},
                       {"socket_path": "rel.sock"}, {"state": "asleep"},
                       {"session": ""}):
            with self.subTest(change), self.assertRaises(ValueError):
                s.put_keeper("ab", **{**good, **change})
        s.put_keeper("ab", **good)
        with self.assertRaises(ValueError):
            s.touch_keeper("ab", state="asleep")


class TestLaunches(StoreCase):
    def test_round_trip_and_settle_once(self) -> None:
        s = self.open()
        launch = s.add_launch("l1a2b", machine="m1", harness="claude",
                              cwd="/w", options={"model": "opus"},
                              command_id="c-launch", session=SESSION)
        self.assertEqual((launch.state, launch.session, launch.options,
                          launch.settled_at),
                         ("pending", SESSION, {"model": "opus"}, None))
        self.assertEqual(s.launch_for_command("c-launch").launch_id, "l1a2b")
        self.assertIsNone(s.launch_for_command("c-other"))
        self.assertTrue(s.settle_launch("l1a2b", note="spawning"))
        self.assertEqual(s.get_launch("l1a2b").state, "pending")
        self.clock.advance(2)
        self.assertTrue(s.settle_launch("l1a2b", "started",
                                        session="claude:new"))
        got = s.get_launch("l1a2b")
        self.assertEqual((got.state, got.session, got.note, got.settled_at),
                         ("started", "claude:new", "spawning", self.clock()))
        # A launch settles once: a late failure cannot undo the start.
        self.assertFalse(s.settle_launch("l1a2b", "failed", note="late"))
        self.assertEqual(s.get_launch("l1a2b").state, "started")
        self.assertFalse(s.settle_launch("nope", "failed"))

    def test_conflicts_and_bad_values(self) -> None:
        s = self.open()
        s.add_launch("l1", machine="m1", harness="claude", cwd="/w",
                     command_id="c1")
        with self.assertRaises(StoreConflict):
            s.add_launch("l1", machine="m1", harness="claude", cwd="/w")
        with self.assertRaises(StoreConflict):
            s.add_launch("l2", machine="m1", harness="claude", cwd="/w",
                         command_id="c1")
        self.assertIsNone(s.get_launch("l2"))
        with self.assertRaises(ValueError):
            s.settle_launch("l1", "pending")
        with self.assertRaises(ValueError):
            s.settle_launch("l1")
        with self.assertRaises(ValueError):
            s.settle_launch("l1", "exploded")
        for change in ({"cwd": ""}, {"command_id": "bad id"},
                       {"session": ""}, {"options": {"provider_key": "x"}}):
            args = {"machine": "m1", "harness": "claude", "cwd": "/w",
                    **change}
            with self.subTest(change), self.assertRaises(ValueError):
                s.add_launch("l3", **args)
        with self.assertRaises(ValueError):
            s.add_launch("l 4", machine="m1", harness="claude", cwd="/w")

    def test_list_by_state(self) -> None:
        s = self.open()
        for n in range(3):
            s.add_launch(f"l{n}", machine="m1", harness="claude", cwd="/w")
            self.clock.advance(1)
        s.settle_launch("l1", "failed", note="no such folder")
        self.assertEqual([x.launch_id for x in s.list_launches()],
                         ["l0", "l1", "l2"])
        self.assertEqual(
            [x.launch_id for x in s.list_launches(states=["pending"])],
            ["l0", "l2"])


class TestReceipts(StoreCase):
    def record(self, s: Store, cid: str = "c1", **kw) -> store_mod.Receipt:
        args = {"session": SESSION, "op": "send", "status": "accepted",
                "payload": {"text": "hello", "mode": "queue"},
                "machine": "m1", "device": "d9f8e7"}
        args.update(kw)
        return s.record_command(cid, **args)

    def test_every_first_status_is_stored_and_answered(self) -> None:
        s = self.open()
        for status in sorted(store_mod.STORED_STATUSES):
            with self.subTest(status):
                r = self.record(s, f"c-{status}", status=status,
                                reason="why" if status != "accepted"
                                else None)
                self.assertIn(r.status, RECEIPT_STATUSES)
                self.assertEqual(r.status, status)
                self.assertFalse(r.duplicate)
                row = s.get_command(f"c-{status}")
                self.assertEqual(row.status, status)
                self.assertEqual(row.device, "d9f8e7")
                self.assertEqual(row.payload,
                                 {"text": "hello", "mode": "queue"})
        held = s.get_command("c-held")
        self.assertEqual(held.expires_at, held.received_at + HOLD_S)
        rejected = s.get_command("c-rejected")
        self.assertEqual(rejected.result,
                         {"op": "send", "ok": False, "note": "why"})
        self.assertEqual(rejected.finished_at, rejected.received_at)
        self.assertEqual(self.record(s, "c-r2", status="rejected").result,
                         {"op": "send", "ok": False})
        self.assertIsNone(s.get_command("c-accepted").result)
        self.assertIsNone(s.get_command("nope"))

    def test_same_id_twice_is_a_duplicate_and_changes_nothing(self) -> None:
        s = self.open()
        first = self.record(s, status="queued", reason="mid-turn")
        before = s.get_command("c1")
        self.clock.advance(30)
        again = self.record(s, status="accepted", device="other1")
        self.assertEqual(
            (again.status, again.duplicate, again.original, again.reason,
             again.result, again.conflict),
            ("duplicate", True, "queued", "mid-turn", None, False))
        self.assertEqual(first.status, "queued")
        self.assertEqual(s.get_command("c1"), before)
        self.assertEqual(
            self.raw().execute("SELECT count(*) FROM commands").fetchone()[0],
            1)

    def test_duplicate_returns_the_stored_result(self) -> None:
        s = self.open()
        self.record(s)
        self.assertTrue(s.set_result("c1", {"ok": True, "note": "sent"}))
        again = self.record(s)
        self.assertEqual(again.original, "accepted")
        self.assertEqual(again.result,
                         {"op": "send", "ok": True, "note": "sent"})

    def test_duplicate_of_a_delivered_hold_answers_accepted(self) -> None:
        s = self.open()
        self.record(s, status="held", reason="machine asleep")
        self.assertTrue(s.transition("c1", "accepted"))
        again = self.record(s, status="held")
        self.assertEqual((again.status, again.original, again.reason),
                         ("duplicate", "accepted", None))

    def test_payload_key_order_does_not_matter(self) -> None:
        s = self.open()
        self.record(s, payload={"text": "hi", "attachments": ["a", "b"]})
        again = self.record(s, payload={"attachments": ["a", "b"],
                                        "text": "hi"})
        self.assertTrue(again.duplicate)

    def test_same_id_for_a_different_command_is_rejected(self) -> None:
        s = self.open()
        self.record(s)
        before = s.get_command("c1")
        for change in ({"payload": {"text": "other", "mode": "queue"}},
                       {"session": "claude:else"}, {"op": "approval"},
                       {"machine": "m2"}, {"payload": None}):
            with self.subTest(change):
                r = self.record(s, **change)
                self.assertEqual((r.status, r.conflict, r.duplicate),
                                 ("rejected", True, False))
                self.assertIn("different command", r.reason)
                self.assertIsNone(r.result)
        self.assertEqual(s.get_command("c1"), before)

    def test_racing_retries_on_two_connections_store_once(self) -> None:
        a, b = self.open(), self.open()
        answers: dict[str, list[store_mod.Receipt]] = {}
        errors: list[BaseException] = []
        gate = threading.Barrier(2)
        lock = threading.Lock()
        ids = [f"race{n}" for n in range(40)]

        def run(s: Store) -> None:
            try:
                for cid in ids:
                    gate.wait()
                    r = self.record(s, cid)
                    with lock:
                        answers.setdefault(cid, []).append(r)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
                gate.abort()

        threads = [threading.Thread(target=run, args=(x,)) for x in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for cid in ids:
            got = sorted(r.status for r in answers[cid])
            self.assertEqual(got, ["accepted", "duplicate"], cid)
        self.assertEqual(
            self.raw().execute("SELECT count(*) FROM commands").fetchone()[0],
            len(ids))

    def test_bad_values_are_refused(self) -> None:
        s = self.open()
        bad = [
            ({"cid": ""}, "command_id"),
            ({"cid": "has space"}, "command_id"),
            ({"cid": "x" * 65}, "command_id"),
            ({"op": "Send"}, "op"),
            ({"op": ""}, "op"),
            ({"status": "duplicate"}, "status"),
            ({"status": "done"}, "status"),
            ({"device": "has space"}, "device"),
            ({"device": 7}, "device"),
            ({"payload": ["a"]}, "object"),
            ({"payload": {"text": {1, 2}}}, "JSON"),
            ({"payload": {"n": float("nan")}}, "JSON"),
            ({"payload": {"auth": {"bearer": "abc"}}}, "credential"),
            ({"payload": {"items": [{"api_key": "sk"}]}}, "credential"),
            ({"payload": {"Device_Token": "t"}}, "credential"),
            ({"payload": {1: "x"}}, "strings"),
            ({"payload": {"text": "x" * (1 << 20)}}, "1 MiB"),
            ({"hold_s": 0}, "hold_s"),
            ({"session": None}, "session"),
        ]
        for change, words in bad:
            change = dict(change)
            cid = change.pop("cid", "c1")
            with self.subTest(change=change, cid=cid):
                with self.assertRaises(ValueError) as caught:
                    self.record(s, cid, **change)
                self.assertIn(words, str(caught.exception))
        self.assertEqual(
            self.raw().execute("SELECT count(*) FROM commands").fetchone()[0],
            0)

    def test_command_id_of_takes_client_id_as_an_alias(self) -> None:
        self.assertEqual(command_id_of({"command_id": "a1"}), "a1")
        self.assertEqual(command_id_of({"client_id": "a1"}), "a1")
        self.assertEqual(command_id_of({"command_id": "a1",
                                        "client_id": "a1"}), "a1")
        self.assertEqual(command_id_of({"command_id": "",
                                        "client_id": "b2"}), "b2")
        self.assertIsNone(command_id_of({}))
        self.assertIsNone(command_id_of({"client_id": ""}))
        with self.assertRaises(ValueError):
            command_id_of({"command_id": "a1", "client_id": "b2"})
        with self.assertRaises(ValueError):
            command_id_of({"client_id": "no spaces"})
        with self.assertRaises(ValueError):
            command_id_of({"command_id": 5})


class TestHeld(StoreCase):
    def hold(self, s: Store, cid: str, machine: str = "m2",
             session: str = "claude:s@m2") -> None:
        r = s.record_command(cid, session=session, op="send", status="held",
                             machine=machine, reason="m2 is asleep",
                             payload={"text": cid})
        self.assertEqual(r.status, "held")

    def test_held_commands_list_in_arrival_order(self) -> None:
        s = self.open()
        # Ids that sort the other way round, and one shared timestamp.
        for cid in ("z9", "a1", "m5"):
            self.hold(s, cid)
        self.hold(s, "other", machine="m3", session="claude:t@m3")
        s.record_command("live", session=SESSION, op="send",
                         status="accepted", machine="m2")
        self.assertEqual([c.command_id for c in s.list_held("m2")],
                         ["z9", "a1", "m5"])
        self.assertEqual([c.command_id for c in s.list_held()],
                         ["z9", "a1", "m5", "other"])
        self.assertEqual([c.payload["text"] for c in s.list_held("m3")],
                         ["other"])
        ids = [c.id for c in s.list_held()]
        self.assertEqual(ids, sorted(ids))

    def test_delivery_claims_each_held_command_once(self) -> None:
        s = self.open()
        self.hold(s, "h1")
        self.hold(s, "h2")
        self.assertTrue(s.transition("h1", "accepted"))
        self.assertFalse(s.transition("h1", "accepted"))
        self.assertTrue(s.transition("h2", "queued"))
        row = s.get_command("h1")
        self.assertEqual((row.status, row.reason, row.expires_at),
                         ("accepted", None, None))
        self.assertEqual(s.list_held("m2"), [])
        self.assertTrue(s.transition("h2", "accepted", reason="turn ended"))
        self.assertEqual(s.get_command("h2").reason, "turn ended")

    def test_held_commands_expire_after_24_hours(self) -> None:
        s = self.open()
        self.hold(s, "h1")
        self.clock.advance(3600)
        self.hold(s, "h2")
        self.clock.advance(HOLD_S - 3600 - 1)
        self.assertEqual(s.expire_held(), [])
        self.assertEqual([c.command_id for c in s.list_held()], ["h1", "h2"])
        self.clock.advance(1)  # h1 is exactly 24 hours old
        # Out of time: never listed for delivery, even before the sweep.
        self.assertEqual([c.command_id for c in s.list_held()], ["h2"])
        expired = s.expire_held()
        self.assertEqual([c.command_id for c in expired], ["h1"])
        self.assertEqual((expired[0].status, expired[0].reason),
                         ("rejected", "expired"))
        self.assertFalse(expired[0].result["ok"])
        self.assertIn("24 hours", expired[0].result["note"])
        self.assertEqual(expired[0].finished_at, self.clock())
        self.assertFalse(s.transition("h1", "accepted"))
        self.assertEqual(s.expire_held(), [])
        self.clock.advance(3600)
        self.assertEqual([c.command_id for c in s.expire_held()], ["h2"])
        # A retry of the expired send is still recognised.
        again = s.record_command("h1", session="claude:s@m2", op="send",
                                 status="held", machine="m2",
                                 payload={"text": "h1"})
        self.assertEqual((again.status, again.original),
                         ("duplicate", "rejected"))

    def test_a_shorter_hold_can_be_asked_for(self) -> None:
        s = self.open()
        s.record_command("h", session=SESSION, op="send", status="held",
                         hold_s=60)
        self.clock.advance(60)
        expired = s.expire_held()
        self.assertEqual([c.command_id for c in expired], ["h"])
        # The note names the row's own hold, not the 24 hour default.
        self.assertIn("within 1 minute", expired[0].result["note"])
        self.assertNotIn("24", expired[0].result["note"])

    def test_a_late_held_command_cannot_be_delivered_before_the_sweep(
            self) -> None:
        s = self.open()
        s.record_command("h", session=SESSION, op="send", status="held",
                         machine="m2", hold_s=5400)
        s.record_command("r", session=SESSION, op="send", status="held",
                         machine="m2", hold_s=5400)
        self.clock.advance(5400)
        self.assertFalse(s.transition("h", "accepted"))
        self.assertFalse(s.transition("h", "queued"))
        self.assertEqual(s.get_command("h").status, "held")
        # Rejecting a late one is still allowed.
        self.assertTrue(s.transition("r", "rejected", reason="session gone"))
        expired = s.expire_held()
        self.assertEqual([c.command_id for c in expired], ["h"])
        self.assertEqual((expired[0].status, expired[0].reason),
                         ("rejected", "expired"))
        self.assertIn("within 90 minutes", expired[0].result["note"])

    def test_span_names_the_hold_for_a_person(self) -> None:
        self.assertEqual(store_mod._span(HOLD_S), "24 hours")
        self.assertEqual(store_mod._span(3600), "1 hour")
        self.assertEqual(store_mod._span(45), "45 seconds")
        self.assertEqual(store_mod._span(1.5), "1.5 seconds")

    def test_transition_rules(self) -> None:
        s = self.open()
        s.record_command("q", session=SESSION, op="send", status="queued")
        s.record_command("a", session=SESSION, op="send", status="accepted")
        s.record_command("r", session=SESSION, op="send", status="rejected")
        self.hold(s, "h")
        self.assertFalse(s.transition("q", "held"))
        self.assertFalse(s.transition("a", "queued"))
        self.assertFalse(s.transition("a", "rejected"))
        self.assertFalse(s.transition("r", "accepted"))
        self.assertFalse(s.transition("nope", "accepted"))
        with self.assertRaises(ValueError):
            s.transition("q", "duplicate")
        with self.assertRaises(ValueError):
            s.transition("q", "accepted", result={"ok": True})
        self.assertTrue(s.transition(
            "q", "rejected", reason="removed",
            result={"ok": False, "cancelled": True,
                    "note": "removed before sending"}))
        q = s.get_command("q")
        self.assertEqual(q.result, {"op": "send", "ok": False,
                                    "cancelled": True,
                                    "note": "removed before sending"})
        self.assertEqual(q.finished_at, self.clock())
        self.assertTrue(s.transition("h", "rejected", reason="session gone"))
        self.assertEqual(s.get_command("h").result,
                         {"op": "send", "ok": False, "note": "session gone"})


class TestResults(StoreCase):
    def test_result_is_stored_once(self) -> None:
        s = self.open()
        s.record_command("a", session=SESSION, op="approval",
                         status="accepted",
                         payload={"request_id": "r1", "decision": "allow"})
        self.clock.advance(4)
        self.assertTrue(s.set_result("a", {"ok": True, "request_id": "r1",
                                           "degraded": None}))
        row = s.get_command("a")
        self.assertEqual(row.result, {"op": "approval", "ok": True,
                                      "request_id": "r1"})
        self.assertEqual(row.finished_at, self.clock())
        self.assertFalse(s.set_result("a", {"ok": False}))
        self.assertTrue(s.get_command("a").result["ok"])

    def test_result_needs_a_running_command(self) -> None:
        s = self.open()
        s.record_command("h", session=SESSION, op="send", status="held")
        s.record_command("r", session=SESSION, op="send", status="rejected")
        s.record_command("q", session=SESSION, op="send", status="queued")
        self.assertFalse(s.set_result("h", {"ok": True}))
        self.assertFalse(s.set_result("r", {"ok": True}))
        self.assertFalse(s.set_result("nope", {"ok": True}))
        self.assertTrue(s.set_result("q", {"ok": True}))
        self.assertEqual(s.get_command("q").status, "queued")

    def test_bad_results_are_refused(self) -> None:
        s = self.open()
        s.record_command("a", session=SESSION, op="send", status="accepted")
        for result in ({"ok": "yes"}, {"note": "x"}, {"ok": True, "token": 1},
                       ["ok"]):
            with self.subTest(result), self.assertRaises(ValueError):
                s.set_result("a", result)
        self.assertIsNone(s.get_command("a").result)

    def test_unfinished_lists_running_commands_without_results(self) -> None:
        s = self.open()
        s.record_command("a1", session=SESSION, op="send", status="accepted",
                         machine="m1")
        s.record_command("q1", session="claude:b", op="send",
                         status="queued", machine="m1")
        s.record_command("a2", session=SESSION, op="send", status="accepted",
                         machine="m2")
        s.record_command("h1", session=SESSION, op="send", status="held")
        s.record_command("r1", session=SESSION, op="send", status="rejected")
        s.set_result("a2", {"ok": True})
        self.assertEqual([c.command_id for c in s.unfinished()],
                         ["a1", "q1"])
        self.assertEqual([c.command_id for c in s.unfinished(session=SESSION)],
                         ["a1"])
        self.assertEqual(s.unfinished(machine="m2"), [])


class TestPrune(StoreCase):
    def test_prune_keeps_held_and_unfinished_and_recent(self) -> None:
        s = self.open()
        s.put_session(SESSION, harness="claude", harness_session_id="0b9e",
                      machine="m1", state="closed")
        s.record_command("old-done", session=SESSION, op="send",
                         status="accepted")
        s.set_result("old-done", {"ok": True})
        s.record_command("old-rejected", session=SESSION, op="send",
                         status="rejected")
        s.record_command("old-running", session=SESSION, op="send",
                         status="accepted")
        s.record_command("old-queued", session=SESSION, op="send",
                         status="queued")
        s.record_command("old-held", session=SESSION, op="send",
                         status="held", hold_s=30 * 86_400)
        s.add_launch("lold", machine="m1", harness="claude", cwd="/w")
        s.settle_launch("lold", "started")
        s.add_launch("lpending", machine="m1", harness="claude", cwd="/w")
        s.put_keeper("kgone", session=SESSION, pid=9, socket_path="/s",
                     ipc_version=1, state="lost")
        s.put_keeper("klive", session=SESSION, pid=10, socket_path="/s2",
                     ipc_version=1, state="live")
        self.clock.advance(8 * 86_400)
        s.record_command("new-done", session=SESSION, op="send",
                         status="accepted")
        s.set_result("new-done", {"ok": True})
        counts = s.prune()
        self.assertEqual(counts, {"commands": 2, "launches": 1,
                                  "keepers": 1})
        left = {r[0] for r in self.raw().execute(
            "SELECT command_id FROM commands")}
        self.assertEqual(left, {"old-running", "old-queued", "old-held",
                                "new-done"})
        self.assertIsNone(s.get_launch("lold"))
        self.assertIsNotNone(s.get_launch("lpending"))
        self.assertIsNone(s.get_keeper("kgone"))
        self.assertIsNotNone(s.get_keeper("klive"))
        self.assertIsNotNone(s.get_session(SESSION))
        self.assertEqual(s.prune(), {"commands": 0, "launches": 0,
                                     "keepers": 0})

    def test_retention_cannot_be_shorter_than_the_hold(self) -> None:
        s = self.open()
        with self.assertRaises(ValueError):
            s.prune(older_than_s=HOLD_S - 1)
        self.assertEqual(s.prune(older_than_s=HOLD_S)["commands"], 0)


class TestBackup(StoreCase):
    def test_backup_includes_a_commit_still_in_the_wal(self) -> None:
        writer = self.open()
        # The schema goes into the main file; the next commit stays in
        # the -wal.
        writer._db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer._db().execute("PRAGMA wal_autocheckpoint = 0")
        writer.record_command("c1", session=SESSION, op="send",
                              status="accepted", payload={"text": "in wal"})
        wal = Path(f"{self.path}-wal")
        self.assertGreater(wal.stat().st_size, 0)
        # Why a file copy is wrong: the main file alone lacks the row.
        copied = self.dir / "copied.sqlite"
        shutil.copyfile(self.path, copied)
        conn = _raw(copied)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM commands").fetchone()[0], 0)
        # A second store (another connection) backs up what the first
        # committed, through the backup API.
        reader = self.open()
        dest = reader.backup(self.dir / "remote-1.sqlite")
        self.assertEqual(_mode(dest), 0o600)
        self.assertEqual(self.siblings(dest), ["remote-1.sqlite"])
        check = self.raw(dest)
        self.assertEqual(check.execute("PRAGMA journal_mode").fetchone()[0],
                         "delete")
        self.assertEqual(
            check.execute("SELECT payload FROM commands").fetchone()[0],
            '{"text":"in wal"}')
        check.close()
        restored = self.open(dest)
        self.assertEqual(restored.get_command("c1").payload,
                         {"text": "in wal"})

    def test_backup_never_overwrites(self) -> None:
        s = self.open()
        dest = self.dir / "taken.sqlite"
        dest.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            s.backup(dest)
        self.assertEqual(dest.read_bytes(), b"keep me")

    def test_a_failed_backup_leaves_nothing(self) -> None:
        s = self.open()
        dest = self.dir / "b.sqlite"
        with mock.patch.object(store_mod.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                s.backup(dest)
        self.assertEqual([n for n in os.listdir(self.dir)
                          if n.startswith((".b.sqlite", "b.sqlite"))], [])


class TestConcurrency(StoreCase):
    def test_concurrent_writers_never_see_database_is_locked(self) -> None:
        stores = [self.open(), self.open()]
        errors: list[BaseException] = []
        per_thread = 40
        gate = threading.Barrier(8)

        def run(s: Store, n: int) -> None:
            try:
                gate.wait()
                for i in range(per_thread):
                    cid = f"t{n}-{i}"
                    s.record_command(cid, session=f"claude:{n}", op="send",
                                     status="accepted",
                                     payload={"text": cid})
                    s.set_result(cid, {"ok": True})
                    s.list_held()
                    s.get_command(cid)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(stores[n % 2], n))
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        conn = self.raw()
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM commands WHERE result IS NOT NULL"
        ).fetchone()[0], 8 * per_thread)
        self.assertEqual(conn.execute(
            "SELECT count(DISTINCT id) FROM commands").fetchone()[0],
            8 * per_thread)

    def test_a_failed_write_rolls_back_and_frees_the_store(self) -> None:
        s = self.open()
        with self.assertRaises(sqlite3.OperationalError):
            with s._write() as db:
                db.execute(
                    "INSERT INTO commands(command_id, op, payload, status, "
                    "received_at, updated_at) VALUES('x','send','{}',"
                    "'accepted',1,1)")
                db.execute("SELECT * FROM no_such_table")
        self.assertIsNone(s.get_command("x"))
        self.assertFalse(s._db().in_transaction)
        # Another thread can still write.
        done = threading.Event()

        def other() -> None:
            s.record_command("y", session=SESSION, op="send",
                             status="accepted")
            done.set()

        t = threading.Thread(target=other)
        t.start()
        t.join()
        self.assertTrue(done.is_set())

    def test_a_failed_commit_ends_the_transaction(self) -> None:
        s = self.open()
        real = s._conn
        calls: list[str] = []

        class FailingCommit:
            in_transaction = True

            def execute(self, sql, *args):
                calls.append(sql)
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                if sql == "ROLLBACK":
                    self.in_transaction = False
                return real.execute(sql, *args)

        s._conn = FailingCommit()  # type: ignore[assignment]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with s._write():
                    pass
        finally:
            s._conn = real
        self.assertEqual(calls, ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"])
        self.assertFalse(real.in_transaction)

    def test_begin_failure_releases_the_lock(self) -> None:
        s = self.open()
        real = s._conn

        class FailingBegin:
            def execute(self, sql, *args):
                raise sqlite3.OperationalError("database is locked")

        s._conn = FailingBegin()  # type: ignore[assignment]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with s._write():
                    pass
        finally:
            s._conn = real
        # The RLock is re-entrant, so ask from another thread.
        got: list[bool] = []

        def probe() -> None:
            ok = s._lock.acquire(blocking=False)
            got.append(ok)
            if ok:
                s._lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        self.assertEqual(got, [True])


if __name__ == "__main__":
    unittest.main()
