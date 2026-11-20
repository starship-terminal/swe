"""The keeper's event log (remote/keeper/log.py): seq stamping, the
delta path, fsync and group commit, segments, the cap, crash recovery
and replay.

Every log here runs with ``background=False`` and a ``FakeClock``, and
its fsync is a recorder, so nothing sleeps or touches the disk cache.
The one flusher-thread test waits on a gate set by the fsync hook.
"""

import json
import os
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

from swe.remote.adapters import events as ev
from swe.remote.adapters.fake import FakeClock
from swe.remote.keeper import log as keeper_log
from swe.remote.keeper.log import (
    EventLog, LogLocked, MARKER_TYPE, SYNC_TYPES, log_dir_for, read_log,
    segment_name,
)
from swe.remote.protocol import key_slug
from tests._tempdir import temp_dir

S = "claude:abc"


def upd(i: int) -> dict:
    """A small non-critical durable event."""
    return ev.tool_updated(S, f"c{i}", text=f"line {i}", ts=1.0)


def crash(log: EventLog) -> None:
    """End ``log`` the way a killed keeper does: fds closed, no flush,
    no clean-shutdown meta. The page cache (the files) survives."""
    with log._lock:
        log._closed = True
        log._release()


class LogBase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_dir(self)
        self.clock = FakeClock()
        self.fsyncs: list[int] = []

    def fsync(self, fd: int) -> None:
        # By inode: fd numbers are reused once a dir fd is closed.
        self.fsyncs.append(os.fstat(fd).st_ino)

    @staticmethod
    def ino(fd: int) -> int:
        return os.fstat(fd).st_ino

    def make(self, **kw) -> EventLog:
        kw.setdefault("home", self.home)
        kw.setdefault("clock", self.clock)
        kw.setdefault("fsync", self.fsync)
        kw.setdefault("boot_id", "boot-a")
        kw.setdefault("background", False)
        log = EventLog(S, **kw)
        self.addCleanup(log.close)
        return log

    def data_syncs(self, log: EventLog) -> int:
        ino = self.ino(log._fd)
        return sum(1 for i in self.fsyncs if i == ino)

    def on_disk(self, log_or_dir) -> list[dict]:
        """Every parseable record in every segment, in file order."""
        d = log_or_dir.dir if isinstance(log_or_dir, EventLog) else log_or_dir
        out = []
        for path in sorted(Path(d).glob("events-*.jsonl")):
            for raw in path.read_bytes().splitlines():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
        return out

    def meta(self, log: EventLog) -> dict:
        return json.loads((log.dir / "meta.json").read_text())


class TestStamping(LogBase):
    def test_stamps_seq_from_one_without_touching_the_input(self):
        log = self.make()
        event = upd(1)
        out = log.emit(event)
        self.assertEqual(out["seq"], 1)
        self.assertEqual(event["seq"], 0)
        self.assertEqual(log.emit(upd(2))["seq"], 2)
        self.assertEqual(log.last_seq, 2)
        self.assertEqual([e["seq"] for e in self.on_disk(log)], [1, 2])
        self.assertEqual(self.on_disk(log)[0]["data"]["call_id"], "c1")

    def test_concurrent_emitters_get_unique_ascending_seqs(self):
        log = self.make()
        delivered: list[int] = []
        log.subscribe(lambda e: delivered.append(e["seq"]))
        threads = [threading.Thread(
            target=lambda: [log.emit(upd(i)) for i in range(150)])
            for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        disk = [e["seq"] for e in self.on_disk(log)]
        self.assertEqual(disk, list(range(1, 1201)))
        self.assertEqual(delivered, disk)

    def test_location_and_modes(self):
        log = self.make()
        log.emit(upd(1))
        self.assertEqual(log.dir, self.home / ".swe" / "data" / "remote"
                         / "keepers" / key_slug(S) / "log")
        self.assertEqual(log.dir, log_dir_for(S, self.home))
        for path in (log.dir, log.dir.parent, log.dir.parent.parent):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in log.dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

    def test_lone_surrogate_text_is_written_escaped(self):
        log = self.make()
        log.emit(ev.message_completed(S, "m1", "assistant", "bad \ud800 end"))
        rep = log.replay()
        self.assertEqual(rep.events[0]["data"]["text"], "bad \ud800 end")

    def test_bad_parameters(self):
        for kw in ({"first_seq": 0}, {"segment_bytes": 0},
                   {"seq_lease": 0}, {"commit_interval": -1},
                   {"segment_bytes": 100, "cap_bytes": 50}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                EventLog(S, home=self.home, background=False, **kw)
        with self.assertRaises(ValueError):
            EventLog("", home=self.home, background=False)

    def test_one_writer_per_log(self):
        log = self.make()
        with self.assertRaises(LogLocked):
            EventLog(S, home=self.home, background=False)
        log.close()
        again = self.make()
        self.assertFalse(again.closed)


class TestDeltas(LogBase):
    def delta(self, text, n, *, offset=None, done=False, msg="m1"):
        return ev.message_delta(S, msg, "assistant", text, n, offset=offset,
                                done=done, turn_id="t1", ts=1.0)

    def test_deltas_reach_subscribers_but_never_disk(self):
        log = self.make()
        got: list[dict] = []
        log.subscribe(got.append)
        log.emit(ev.turn_started(S, "t1"))
        out = log.emit(self.delta("Hel", 1))
        self.assertNotIn("seq", out)
        log.emit(self.delta("lo", 2, offset=3))
        self.assertEqual([e["type"] for e in got],
                         ["turn.started", "message.delta", "message.delta"])
        self.assertEqual([e["type"] for e in self.on_disk(log)],
                         ["turn.started"])
        self.assertEqual(log.last_seq, 1)
        self.assertEqual(log.stats()["deltas"], 2)

    def test_live_text_is_snapshotted_and_cleared_on_completion(self):
        log = self.make()
        log.emit(ev.turn_started(S, "t1"))
        log.emit(self.delta("Hel", 1))
        log.emit(self.delta("lo", 2, offset=3))
        # A resent chunk at an earlier offset replaces, never doubles.
        log.emit(self.delta("lo wor", 3, offset=3, done=True))
        sub = log.subscribe(lambda e: None)
        self.assertEqual(sub.last_seq, 1)
        self.assertEqual(sub.open_turn, "t1")
        self.assertEqual(sub.live, [{"id": "m1", "kind": "assistant",
                                     "text": "Hello wor", "turn_id": "t1",
                                     "done": True}])
        log.emit(ev.message_completed(S, "m1", "assistant", "Hello wor"))
        self.assertEqual(log.subscribe(lambda e: None).live, [])
        log.emit(self.delta("x", 1, msg="m2"))
        log.emit(ev.turn_ended(S, "t1", "completed"))
        later = log.subscribe(lambda e: None)
        self.assertEqual(later.live, [])
        self.assertIsNone(later.open_turn)

    def test_session_at_rest_clears_live_text(self):
        log = self.make()
        log.emit(self.delta("x", 1))
        log.emit(ev.tool_started(S, "call9", "Bash"))
        log.emit(ev.session_state(S, "agent_down"))
        self.assertEqual(log.subscribe(lambda e: None).live, [])


class TestSubscribers(LogBase):
    def test_pending_requests_follow_the_events(self):
        log = self.make()
        log.emit(ev.approval_requested(S, "r1", "tool", "Run ls?"))
        log.emit(ev.input_requested(S, "r2", "Which?"))
        log.emit(ev.approval_resolved(S, "r1", "user", decision="allow"))
        sub = log.subscribe(lambda e: None)
        self.assertEqual([p["data"]["request_id"] for p in sub.pending],
                         ["r2"])

    def test_close_stops_delivery(self):
        log = self.make()
        got: list[dict] = []
        sub = log.subscribe(got.append)
        log.emit(upd(1))
        sub.close()
        sub.close()
        log.emit(upd(2))
        self.assertEqual([e["seq"] for e in got], [1])

    def test_a_failing_subscriber_is_dropped_and_others_still_hear(self):
        log = self.make()
        got: list[int] = []

        def boom(e):
            raise RuntimeError("client gone")

        log.subscribe(boom)
        log.subscribe(lambda e: got.append(e["seq"]))
        with self.assertLogs("swe.remote.keeper.log", "ERROR"):
            log.emit(upd(1))
        log.emit(upd(2))
        self.assertEqual(got, [1, 2])
        self.assertEqual(len(log._subs), 1)

    def test_a_subscriber_may_not_emit_or_subscribe(self):
        log = self.make()
        errors: list[str] = []

        def reenter(e):
            for call in (lambda: log.emit(upd(99)),
                         lambda: log.subscribe(print)):
                try:
                    call()
                except RuntimeError as exc:
                    errors.append(str(exc))

        log.subscribe(reenter)
        log.emit(upd(1))
        self.assertEqual(len(errors), 2)
        self.assertEqual(log.last_seq, 1)

    def test_a_callback_may_close_its_own_subscription(self):
        log = self.make()
        first: list[int] = []
        second: list[int] = []
        holder: list = []

        def once(e):
            first.append(e["seq"])
            holder[0].close()  # would deadlock if it took the lock

        holder.append(log.subscribe(once))
        log.subscribe(lambda e: second.append(e["seq"]))
        log.emit(upd(1))
        log.emit(upd(2))
        self.assertEqual(first, [1])
        self.assertEqual(second, [1, 2])
        self.assertEqual(len(log._subs), 1)

    def test_subscribe_after_close_raises(self):
        log = self.make()
        log.close()
        with self.assertRaises(ValueError):
            log.subscribe(print)


class TestFsync(LogBase):
    CRITICAL = {
        "command.receipt": lambda: ev.command_receipt(S, "c1", "accepted"),
        "approval.requested": lambda: ev.approval_requested(
            S, "r1", "tool", "Run ls?"),
        "approval.resolved": lambda: ev.approval_resolved(
            S, "r1", "user", decision="allow"),
        "input.requested": lambda: ev.input_requested(S, "r2", "Which?"),
        "turn.started": lambda: ev.turn_started(S, "t1"),
        "turn.ended": lambda: ev.turn_ended(S, "t1", "completed"),
        "message.completed": lambda: ev.message_completed(
            S, "m1", "assistant", "done"),
        "checkpoint.created": lambda: ev.checkpoint_created(S, "k1"),
        "checkpoint.restored": lambda: ev.checkpoint_restored(S, "k1"),
    }

    def test_every_critical_type_is_fsynced_before_delivery(self):
        self.assertEqual(set(self.CRITICAL), SYNC_TYPES)
        log = self.make()
        log.emit(upd(0))  # opens the segment; not synced
        self.assertEqual(self.data_syncs(log), 0)
        seen_at: list[int] = []
        log.subscribe(lambda e: seen_at.append(self.data_syncs(log)))
        for n, (etype, build) in enumerate(self.CRITICAL.items(), 1):
            with self.subTest(etype=etype):
                log.emit(build())
                self.assertEqual(self.data_syncs(log), n)
                self.assertEqual(seen_at[-1], n)  # synced, then delivered

    def test_other_types_wait_for_group_commit(self):
        log = self.make()
        for event in (upd(1), ev.usage_updated(S, input_tokens=3),
                      ev.session_state(S, "running"),
                      ev.tool_started(S, "c1", "Bash")):
            log.emit(event)
        self.assertEqual(self.data_syncs(log), 0)
        self.assertEqual(log.stats()["syncs"], 0)

    def test_group_commit_follows_the_clock(self):
        log = self.make()
        log.emit(upd(1))
        self.assertFalse(log.tick())
        self.clock.advance(0.1)
        log.emit(upd(2))
        self.assertFalse(log.tick())
        self.clock.advance(0.15)  # 0.25 s since the first unsynced write
        self.assertTrue(log.tick())
        self.assertEqual(self.data_syncs(log), 1)
        self.assertFalse(log.tick())  # nothing left to sync

    def test_a_late_emit_commits_inline(self):
        log = self.make()
        log.emit(upd(1))
        self.clock.advance(0.3)
        log.emit(upd(2))
        self.assertEqual(self.data_syncs(log), 1)

    def test_flush_syncs_now(self):
        log = self.make()
        log.emit(upd(1))
        log.flush()
        self.assertEqual(self.data_syncs(log), 1)
        log.flush()
        self.assertEqual(self.data_syncs(log), 1)

    def test_background_flusher_commits_without_a_tick(self):
        synced = threading.Event()
        holder: list[EventLog] = []

        def fsync(fd):
            if holder and fd == holder[0]._fd:
                synced.set()

        log = EventLog(S, home=self.home, fsync=fsync, commit_interval=0.01,
                       boot_id="boot-a")
        self.addCleanup(log.close)
        holder.append(log)
        log.emit(upd(1))
        self.assertTrue(synced.wait(5.0))
        log.close()
        self.assertFalse(log._thread.is_alive())

    def test_fsync_failure_is_reported_and_the_event_still_delivered(self):
        errors: list = []
        log = self.make(on_error=lambda exc, event: errors.append(
            (exc, event and event["type"])))
        log.emit(upd(1))
        got: list[dict] = []
        log.subscribe(got.append)
        log._fsync = mock.Mock(side_effect=OSError(5, "EIO"))
        out = log.emit(ev.turn_started(S, "t1"))
        log._fsync = self.fsync
        self.assertEqual(out["seq"], 2)
        self.assertEqual(got[-1]["seq"], 2)
        self.assertEqual(errors[0][1], "turn.started")
        self.assertEqual(log.stats()["errors"], 1)


class TestSegments(LogBase):
    def test_rotation_names_segments_by_their_first_seq(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        for i in range(1, 21):
            log.emit(upd(i))
        segs = log.segments()
        self.assertGreater(len(segs), 3)
        for first, path, size in segs:
            self.assertEqual(path.name, segment_name(first))
            records = [json.loads(r) for r in path.read_bytes().splitlines()]
            self.assertEqual(records[0]["seq"], first)
            self.assertLessEqual(size, 400)
            self.assertEqual(size, path.stat().st_size)
        rep = log.replay()
        self.assertEqual([e["seq"] for e in rep.events], list(range(1, 21)))
        self.assertFalse(rep.truncated)

    def test_a_full_segment_is_sealed_with_an_fsync(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        first_fd = None
        while len(log.segments()) < 2:
            first = self.ino(log._fd) if log._fd is not None else None
            log.emit(upd(1))
        self.assertIn(first, self.fsyncs)

    def test_an_oversized_event_gets_a_segment_of_its_own(self):
        log = self.make(segment_bytes=400, cap_bytes=4000)
        log.emit(upd(1))
        log.emit(ev.message_completed(S, "m1", "assistant", "x" * 1000))
        log.emit(upd(3))
        sizes = [s[2] for s in log.segments()]
        self.assertEqual(len(sizes), 3)
        self.assertGreater(sizes[1], 400)

    def test_cap_drops_oldest_segments_and_records_the_marker(self):
        log = self.make(segment_bytes=400, cap_bytes=1200)
        for i in range(1, 61):
            log.emit(upd(i))
        segs = log.segments()
        self.assertLessEqual(sum(s[2] for s in segs), 1200)
        self.assertEqual(len(list(log.dir.glob("events-*.jsonl"))), len(segs))
        marker = log.marker
        self.assertEqual(marker["type"], MARKER_TYPE)
        self.assertEqual(marker["before_seq"], segs[0][0])
        self.assertGreater(marker["segments"], 0)
        self.assertGreater(marker["bytes"], 0)
        self.assertEqual(self.meta(log)["truncated"], marker)
        self.assertEqual(log.oldest_seq, segs[0][0])

        rep = log.replay(0)
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "evicted")
        self.assertEqual(rep.marker, marker)
        self.assertEqual(rep.events[0]["seq"], marker["before_seq"])
        self.assertEqual(rep.events[-1]["seq"], 60)
        self.assertFalse(log.replay(marker["before_seq"] - 1).truncated)

    def test_open_finishes_an_interrupted_eviction(self):
        log = self.make(segment_bytes=400, cap_bytes=1200)
        for i in range(1, 61):
            log.emit(upd(i))
        old = log.segments()[0]
        log.close()
        # A crash after the marker was written but before the unlink.
        stale = log.dir / segment_name(1)
        stale.write_bytes((json.dumps(dict(upd(1), seq=1)) + "\n").encode())
        again = self.make(segment_bytes=400, cap_bytes=1200)
        self.assertFalse(stale.exists())
        self.assertEqual(again.segments()[0][0], old[0])

    def test_open_applies_a_smaller_cap(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        for i in range(1, 61):
            log.emit(upd(i))
        log.close()
        again = self.make(segment_bytes=400, cap_bytes=1200)
        self.assertLessEqual(again.stats()["bytes"], 1200)
        self.assertIsNotNone(again.marker)
        self.assertEqual(again.emit(upd(61))["seq"], 61)


class TestRecovery(LogBase):
    def segment(self, log: EventLog) -> Path:
        return log.segments()[-1][1]

    def test_reopen_continues_at_last_seq_plus_one(self):
        log = self.make()
        for i in range(1, 4):
            log.emit(upd(i))
        log.close()
        self.assertEqual(self.meta(log)["reserved"], 3)
        again = self.make(boot_id="boot-b")
        self.assertEqual(again.recovery.last_seq, 3)
        self.assertEqual(again.recovery.seq_gap, 0)
        self.assertEqual(again.emit(upd(4))["seq"], 4)
        self.assertEqual([e["seq"] for e in again.replay().events],
                         [1, 2, 3, 4])

    def test_torn_last_line_is_cut_and_its_seq_is_not_lost(self):
        log = self.make()
        for i in range(1, 4):
            log.emit(upd(i))
        path = self.segment(log)
        good = path.read_bytes()
        crash(log)
        with open(path, "ab") as fh:
            fh.write(b'{"v":2,"type":"tool.updated","sess')
        again = self.make()
        self.assertEqual(again.recovery.torn_bytes, len(
            b'{"v":2,"type":"tool.updated","sess'))
        self.assertEqual(again.recovery.last_seq, 3)
        self.assertEqual(path.read_bytes(), good)
        self.assertEqual(again.emit(upd(4))["seq"], 4)
        self.assertEqual([e["seq"] for e in self.on_disk(again)],
                         [1, 2, 3, 4])

    def test_a_complete_line_missing_its_newline_is_torn_too(self):
        log = self.make()
        log.emit(upd(1))
        path = self.segment(log)
        crash(log)
        with open(path, "ab") as fh:
            fh.write(json.dumps(dict(upd(2), seq=2)).encode())
        again = self.make()
        self.assertEqual(again.recovery.last_seq, 1)
        self.assertEqual(again.emit(upd(2))["seq"], 2)
        self.assertEqual(len(path.read_bytes().splitlines()), 2)

    def test_a_segment_left_only_garbage_is_removed(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        while len(log.segments()) < 2:
            log.emit(upd(1))
        last_seq = log.last_seq
        first, path, _ = log.segments()[-1]
        crash(log)
        path.write_bytes(b"\x00" * 64)  # a power cut's zero-filled block
        again = self.make(segment_bytes=400, cap_bytes=1 << 20)
        self.assertFalse(path.exists())
        self.assertEqual(again.recovery.last_seq, last_seq - 1)
        self.assertEqual(again.recovery.torn_bytes, 64)
        self.assertEqual(again.emit(upd(1))["seq"], last_seq)

    def test_garbage_mid_file_is_kept_and_skipped(self):
        log = self.make()
        for i in range(1, 6):
            log.emit(upd(i))
        path = self.segment(log)
        log.close()
        lines = path.read_bytes().splitlines(keepends=True)
        lines.insert(2, b"not json at all\n")
        lines.insert(4, json.dumps(dict(upd(9), seq=1)).encode() + b"\n")
        damaged = b"".join(lines)
        path.write_bytes(damaged)
        again = self.make()
        self.assertEqual(again.recovery.skipped_lines, 2)
        self.assertEqual(again.recovery.torn_bytes, 0)
        self.assertEqual(path.read_bytes(), damaged)  # left as evidence
        rep = again.replay()
        self.assertEqual([e["seq"] for e in rep.events], [1, 2, 3, 4, 5])
        self.assertEqual(rep.skipped, 1)  # the out-of-order one is dropped
        self.assertEqual(again.emit(upd(6))["seq"], 6)

    def test_a_crash_on_the_same_boot_leaves_no_gap(self):
        log = self.make()
        for i in range(1, 4):
            log.emit(upd(i))
        crash(log)
        again = self.make(boot_id="boot-a")
        self.assertEqual(again.recovery.seq_gap, 0)
        self.assertEqual(again.emit(upd(4))["seq"], 4)

    def test_a_crash_on_another_boot_skips_the_lease(self):
        log = self.make(seq_lease=10)
        for i in range(1, 4):
            log.emit(upd(i))
        crash(log)
        again = self.make(boot_id="boot-b", seq_lease=10)
        self.assertEqual(again.recovery.last_seq, 3)
        self.assertEqual(again.recovery.seq_gap, 7)
        self.assertEqual(again.last_seq, 10)
        self.assertEqual(again.emit(upd(4))["seq"], 11)
        rep = again.replay(5)
        self.assertFalse(rep.truncated)
        self.assertEqual([e["seq"] for e in rep.events], [11])

    def test_an_unknown_boot_counts_as_another_boot(self):
        log = self.make(seq_lease=10, boot_id="")
        log.emit(upd(1))
        crash(log)
        again = self.make(seq_lease=10, boot_id="")
        self.assertEqual(again.emit(upd(2))["seq"], 11)

    def test_the_lease_is_on_disk_before_a_seq_past_it(self):
        log = self.make(seq_lease=3)
        reserved = []
        for i in range(1, 8):
            log.emit(upd(i))
            reserved.append(self.meta(log)["reserved"])
        self.assertTrue(all(r >= i for i, r in enumerate(reserved, 1)))
        crash(log)
        again = self.make(seq_lease=3, boot_id="boot-b")
        self.assertGreater(again.emit(upd(8))["seq"], 7)

    def test_a_lost_meta_file_still_reopens(self):
        log = self.make()
        log.emit(upd(1))
        log.close()
        (log.dir / "meta.json").unlink()
        again = self.make(boot_id="boot-b")
        self.assertEqual(again.emit(upd(2))["seq"], 2)
        (log.dir / "meta.json").write_text("{not json")
        again.close()
        third = self.make()
        self.assertEqual(third.recovery.last_seq, 2)

    def test_open_turn_and_pending_requests_are_rebuilt(self):
        log = self.make()
        log.emit(ev.turn_started(S, "t0"))
        log.emit(ev.approval_requested(S, "r0", "tool", "old"))
        log.emit(ev.approval_resolved(S, "r0", "turn_ended"))
        log.emit(ev.turn_ended(S, "t0", "completed"))
        log.emit(ev.turn_started(S, "t1"))
        log.emit(ev.approval_requested(S, "r1", "tool", "Run ls?"))
        log.emit(ev.input_requested(S, "r2", "Which?"))
        log.emit(ev.approval_resolved(S, "r1", "user", decision="allow"))
        log.emit(upd(1))
        crash(log)
        again = self.make()
        self.assertEqual(again.recovery.open_turn, "t1")
        self.assertEqual([p["data"]["request_id"]
                          for p in again.recovery.pending], ["r2"])
        sub = again.subscribe(lambda e: None)
        self.assertEqual(sub.open_turn, "t1")
        self.assertEqual([p["data"]["request_id"] for p in sub.pending],
                         ["r2"])

    def test_a_finished_turn_leaves_nothing_open(self):
        log = self.make()
        log.emit(ev.turn_started(S, "t1"))
        log.emit(ev.turn_ended(S, "t1", "completed"))
        log.emit(ev.session_state(S, "idle"))
        log.close()
        again = self.make()
        self.assertIsNone(again.recovery.open_turn)
        self.assertEqual(again.recovery.pending, ())

    def test_requests_outside_a_turn_are_found_in_older_segments(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        log.emit(ev.input_requested(S, "r9", "Name?"))
        while len(log.segments()) < 3:
            log.emit(upd(1))
        log.close()
        again = self.make(segment_bytes=400, cap_bytes=1 << 20)
        self.assertIsNone(again.recovery.open_turn)
        self.assertEqual([p["data"]["request_id"]
                          for p in again.recovery.pending], ["r9"])


class TestFirstSeq(LogBase):
    def test_a_new_log_starts_at_first_seq(self):
        log = self.make(first_seq=1001)
        self.assertEqual(log.last_seq, 1000)
        self.assertEqual(log.emit(upd(1))["seq"], 1001)
        self.assertEqual(log.segments()[0][1].name, segment_name(1001))
        self.assertEqual(log.first_seq, 1001)
        rep = log.replay(0)
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "before_start")
        self.assertEqual(rep.oldest_seq, 1001)
        self.assertEqual([e["seq"] for e in rep.events], [1001])
        self.assertFalse(log.replay(1000).truncated)

    def test_first_seq_only_moves_an_existing_log_forward(self):
        log = self.make(first_seq=1001)
        log.emit(upd(1))
        log.close()
        back = self.make(first_seq=5)
        self.assertEqual(back.emit(upd(2))["seq"], 1002)
        self.assertEqual(back.first_seq, 1001)
        back.close()
        ahead = self.make(first_seq=5000)
        self.assertEqual(ahead.emit(upd(3))["seq"], 5000)
        self.assertEqual([e["seq"] for e in ahead.replay(0).events],
                         [1001, 1002, 5000])

    def test_an_empty_log_reopens_at_its_first_seq(self):
        log = self.make(first_seq=40)
        log.close()
        again = self.make()
        self.assertEqual(again.emit(upd(1))["seq"], 40)


class TestReplay(LogBase):
    def filled(self, n=30) -> EventLog:
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        for i in range(1, n + 1):
            log.emit(upd(i))
        return log

    def test_replay_from_a_seq_across_segments(self):
        log = self.filled()
        self.assertGreater(len(log.segments()), 3)
        for after in (0, 1, 7, 13, 29, 30):
            with self.subTest(after=after):
                rep = log.replay(after)
                self.assertEqual([e["seq"] for e in rep.events],
                                 list(range(after + 1, 31)))
                self.assertFalse(rep.truncated)

    def test_until_and_limit_page_through(self):
        log = self.filled()
        rep = log.replay(5, until_seq=12)
        self.assertEqual([e["seq"] for e in rep.events], list(range(6, 13)))
        self.assertFalse(rep.more)
        pages, after = [], 0
        while True:
            page = log.replay(after, limit=7)
            pages.append([e["seq"] for e in page.events])
            if not page.more:
                break
            after = page.events[-1]["seq"]
        self.assertEqual(sum(pages, []), list(range(1, 31)))
        self.assertEqual(len(pages[0]), 7)

    def test_replay_stops_at_the_last_seq_when_called(self):
        log = self.filled(3)
        self.assertEqual(len(log.replay(0, until_seq=99).events), 3)

    def test_replay_of_an_empty_log(self):
        log = self.make()
        rep = log.replay(0)
        self.assertEqual((rep.events, rep.truncated, rep.oldest_seq),
                         ([], False, 1))

    def test_read_log_works_beside_a_live_writer(self):
        log = self.filled()
        # A half-written line at the tail, as a reader may catch one.
        with open(log.segments()[-1][1], "ab") as fh:
            fh.write(b'{"v":2,"ty')
        rep = read_log(S, 10, home=self.home)
        self.assertEqual([e["seq"] for e in rep.events], list(range(11, 31)))
        self.assertEqual(read_log(root=log.dir).events[0]["seq"], 1)
        with self.assertRaises(ValueError):
            read_log()

    def test_read_log_after_eviction(self):
        log = self.make(segment_bytes=400, cap_bytes=1200)
        for i in range(1, 61):
            log.emit(upd(i))
        log.close()
        rep = read_log(S, 0, home=self.home)
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "evicted")
        self.assertEqual(rep.events[0]["seq"], rep.marker["before_seq"])

    def test_a_segment_dropped_under_a_lock_free_reader(self):
        log = self.filled()
        segs = [(s[0], s[1]) for s in log.segments()]
        segs[0][1].unlink()
        handles, lost = keeper_log._open_segments(segs, 0)
        try:
            rep = keeper_log._replay(handles, 0, None, None, first_seq=1,
                                     marker=None, lost_before=lost)
        finally:
            for _, fh in handles:
                fh.close()
        self.assertEqual(lost, segs[1][0])
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "evicted")
        self.assertEqual(rep.events[0]["seq"], segs[1][0])

    def test_read_log_of_a_missing_log(self):
        rep = read_log("nobody", home=self.home)
        self.assertEqual((rep.events, rep.truncated), ([], False))


class TestValidation(LogBase):
    def test_invalid_events_are_rejected_and_recorded_once_per_type(self):
        log = self.make()
        got: list[dict] = []
        log.subscribe(got.append)
        bad = upd(1)
        bad["data"]["typo"] = 1
        with self.assertLogs("swe.remote.keeper.log", "WARNING"):
            self.assertIsNone(log.emit(bad))
            self.assertIsNone(log.emit(dict(bad)))
        disk = self.on_disk(log)
        self.assertEqual([e["type"] for e in disk], ["error"])
        self.assertEqual(disk[0]["data"]["code"], "invalid_event")
        self.assertIn("tool.updated", disk[0]["data"]["message"])
        self.assertIn("typo", disk[0]["data"]["message"])
        self.assertEqual([e["type"] for e in got], ["error"])
        self.assertEqual(log.stats()["rejected"], 2)

    def test_invalid_deltas_and_non_events_are_rejected(self):
        log = self.make()
        delta = ev.message_delta(S, "m1", "assistant", "x", 1)
        with self.assertLogs("swe.remote.keeper.log", "WARNING"):
            self.assertIsNone(log.emit(dict(delta, seq=3)))
            self.assertIsNone(log.emit({"type": 7}))
        errors = [e for e in self.on_disk(log) if e["type"] == "error"]
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[1]["session"], S)

    def test_after_close_events_are_dropped(self):
        log = self.make()
        log.close()
        self.assertIsNone(log.emit(upd(1)))
        self.assertIsNone(log.emit(ev.message_delta(S, "m", "assistant",
                                                    "x", 1)))
        with self.assertLogs("swe.remote.keeper.log", "WARNING"):
            bad = upd(2)
            bad["data"]["typo"] = 1
            self.assertIsNone(log.emit(bad))
        self.assertEqual(log.stats()["dropped"], 2)


class TestClose(LogBase):
    def test_close_flushes_and_records_a_clean_shutdown(self):
        log = self.make(seq_lease=100)
        log.emit(upd(1))
        log.emit(upd(2))
        self.assertEqual(self.meta(log)["reserved"], 100)
        ino = self.ino(log._fd)
        self.fsyncs.clear()
        with log:
            pass
        self.assertIn(ino, self.fsyncs)
        self.assertEqual(log.stats()["syncs"], 1)
        self.assertEqual(self.meta(log)["reserved"], 2)
        self.assertTrue(log.closed)
        log.close()  # idempotent

    def test_close_with_nothing_written(self):
        log = self.make()
        log.close()
        self.assertEqual(self.meta(log)["reserved"], 0)


class TestWriteFailures(LogBase):
    def test_a_failed_write_is_rolled_back_and_its_seq_reused(self):
        errors: list = []
        log = self.make(on_error=lambda exc, event: errors.append(event))
        log.emit(upd(1))
        got: list[dict] = []
        log.subscribe(got.append)
        with mock.patch.object(keeper_log, "_write_all",
                               side_effect=OSError(28, "ENOSPC")):
            self.assertIsNone(log.emit(upd(2)))
        self.assertEqual(errors[0]["seq"], 2)
        self.assertEqual(got, [])
        self.assertEqual(log.emit(upd(3))["seq"], 2)
        self.assertEqual([e["seq"] for e in self.on_disk(log)], [1, 2])

    def test_a_partial_write_that_cannot_be_cut_is_fenced_off(self):
        log = self.make(on_error=lambda exc, event: None)
        log.emit(upd(1))
        real = keeper_log._write_all

        def partial(fd, data):
            real(fd, data[:10])
            raise OSError(28, "ENOSPC")

        with mock.patch.object(keeper_log, "_write_all", side_effect=partial), \
                mock.patch.object(keeper_log.os, "ftruncate",
                                  side_effect=OSError(5, "EIO")):
            self.assertIsNone(log.emit(upd(2)))
        self.assertTrue(log._needs_newline)
        self.assertEqual(log.emit(upd(3))["seq"], 2)
        self.assertEqual(log.stats()["bytes"],
                         log.segments()[-1][1].stat().st_size)
        log.close()
        again = self.make()
        self.assertEqual(again.recovery.skipped_lines, 1)
        self.assertEqual([e["seq"] for e in again.replay().events], [1, 2])

    def test_a_failed_lease_write_holds_the_seq_back(self):
        errors: list = []
        log = self.make(seq_lease=1,
                        on_error=lambda exc, event: errors.append(exc))
        log.emit(upd(1))
        with mock.patch.object(keeper_log.os, "replace",
                               side_effect=OSError(28, "ENOSPC")):
            self.assertIsNone(log.emit(upd(2)))
        self.assertEqual(log._reserved, 1)
        self.assertEqual(log.emit(upd(3))["seq"], 2)
        self.assertEqual(self.meta(log)["reserved"], 2)

    def test_errors_go_to_the_logger_without_a_handler(self):
        log = self.make()
        with mock.patch.object(keeper_log, "_write_all",
                               side_effect=OSError(28, "ENOSPC")), \
                self.assertLogs("swe.remote.keeper.log", "WARNING"):
            log.emit(upd(1))

    def test_a_broken_error_handler_still_logs(self):
        def handler(exc, event):
            raise RuntimeError("handler bug")

        log = self.make(on_error=handler)
        with mock.patch.object(keeper_log, "_write_all",
                               side_effect=OSError(28, "ENOSPC")), \
                self.assertLogs("swe.remote.keeper.log", "WARNING") as cm:
            log.emit(upd(1))
        self.assertTrue(any("on_error failed" in m for m in cm.output))

    def test_tick_and_flush_report_fsync_failures(self):
        errors: list = []
        log = self.make(on_error=lambda exc, event: errors.append(exc))
        log.emit(upd(1))
        self.clock.advance(1)
        log._fsync = mock.Mock(side_effect=OSError(5, "EIO"))
        self.assertFalse(log.tick())
        log.flush()
        self.assertEqual(len(errors), 2)


class TestEdges(LogBase):
    def test_what_counts_as_a_record(self):
        parse = keeper_log._parse
        self.assertIsNotNone(parse(b'{"type":"x","seq":1}\n'))
        for raw in (b"[1, 2]\n", b'{"seq":1}\n', b'{"type":"x","seq":true}\n',
                    b'{"type":"x","seq":0}\n', b'{"type":"x"}\n', b"\n",
                    b"\xff\xfe\n"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse(raw))

    def test_a_meta_file_of_another_version_is_ignored(self):
        log = self.make(first_seq=7)
        log.emit(upd(1))
        log.close()
        (log.dir / "meta.json").write_text('{"v": 99, "first_seq": 500}')
        again = self.make()
        self.assertEqual(again.first_seq, 7)  # from the oldest segment
        self.assertEqual(again.emit(upd(2))["seq"], 8)

    def test_read_log_ignores_segments_an_eviction_left(self):
        log = self.make(segment_bytes=400, cap_bytes=1200)
        for i in range(1, 61):
            log.emit(upd(i))
        log.close()
        stale = log.dir / segment_name(1)
        stale.write_bytes((json.dumps(dict(upd(1), seq=1)) + "\n").encode())
        rep = read_log(root=log.dir)
        self.assertEqual(rep.events[0]["seq"], rep.marker["before_seq"])
        self.assertTrue(stale.exists())  # a reader repairs nothing

    def test_a_loose_folder_is_tightened(self):
        d = log_dir_for(S, self.home)
        d.mkdir(parents=True, mode=0o755)
        os.chmod(d, 0o755)
        log = self.make()
        self.assertEqual(stat.S_IMODE(log.dir.stat().st_mode), 0o700)

    def test_a_failed_open_releases_the_lock(self):
        with mock.patch.object(EventLog, "_recover",
                               side_effect=OSError(5, "EIO")):
            with self.assertRaises(OSError):
                EventLog(S, home=self.home, background=False)
        self.make()  # the lock is free again


class TestReviewRound1(LogBase):
    """Numbering and replay holes found in review of the first cut."""

    def test_a_crash_gap_is_never_undone_by_later_opens(self):
        log = self.make(seq_lease=10)
        log.emit(upd(1))
        crash(log)
        b = self.make(seq_lease=10, boot_id="boot-b")
        self.assertEqual(b.recovery.next_seq, 11)
        b.close()  # clean, nothing written
        self.assertEqual(self.meta(b)["floor"], 11)
        c = self.make(seq_lease=10, boot_id="boot-b")
        self.assertEqual(c.recovery.next_seq, 11)
        crash(c)  # unclean, nothing written
        d = self.make(seq_lease=10, boot_id="boot-b")
        self.assertEqual(d.recovery.next_seq, 11)
        crash(d)
        e = self.make(seq_lease=10, boot_id="boot-c")
        self.assertGreaterEqual(e.recovery.next_seq, 11)
        self.assertGreaterEqual(e.emit(upd(2))["seq"], 11)
        e.close()
        f = self.make(seq_lease=10, boot_id="boot-c")
        self.assertGreater(f.emit(upd(3))["seq"], 11)

    def test_evicting_every_sealed_segment_cannot_reset_numbering(self):
        log = self.make(segment_bytes=400, cap_bytes=400,
                        on_error=lambda exc, event: None)
        log.emit(upd(1))
        while True:
            size = log.segments()[-1][2]
            line = keeper_log._encode(dict(upd(1), seq=log.last_seq + 1))
            if size + len(line) > 400:
                break
            log.emit(upd(1))
        written = log.last_seq
        real = keeper_log._write_all

        def fail_events(fd, data):
            if data.startswith(b'{"v":2'):
                raise OSError(28, "ENOSPC")
            real(fd, data)

        # The append rotates (sealing and evicting every older file),
        # then its write fails: only an empty segment is left.
        with mock.patch.object(keeper_log, "_write_all",
                               side_effect=fail_events):
            self.assertIsNone(log.emit(upd(1)))
        self.assertEqual(log.marker["before_seq"], written + 1)
        self.assertEqual([s[2] for s in log.segments()], [0])
        crash(log)
        again = self.make(segment_bytes=400, cap_bytes=400)
        self.assertEqual(again.segments(), [])
        self.assertEqual(again.recovery.next_seq, written + 1)
        self.assertEqual(again.emit(upd(1))["seq"], written + 1)

    def evicting_reader(self, *, every_time: bool):
        """A writer that rotates and evicts right after the reader lists
        the folder: the first listing (or every one) is stale."""
        log = self.make(segment_bytes=400, cap_bytes=1200)
        for i in range(1, 31):
            log.emit(upd(i))
        real = keeper_log._list_segments
        stale: list = []

        def listing(d):
            if stale and every_time:
                return list(stale)
            out = real(d)
            if not stale:
                stale.extend(out)
                for i in range(31, 91):  # every listed file is evicted
                    log.emit(upd(i))
            return out

        with mock.patch.object(keeper_log, "_list_segments",
                               side_effect=listing):
            rep = read_log(S, 0, home=self.home)
        self.assertFalse(any(p.exists() for _, p in stale))
        return log, rep

    def test_read_log_relists_when_a_writer_evicts_under_it(self):
        log, rep = self.evicting_reader(every_time=False)
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "evicted")
        self.assertEqual(rep.events[0]["seq"], log.marker["before_seq"])
        self.assertEqual([e["seq"] for e in rep.events],
                         list(range(log.marker["before_seq"], 91)))

    def test_a_stale_listing_still_reports_the_loss(self):
        _, rep = self.evicting_reader(every_time=True)
        self.assertEqual(rep.events, [])
        self.assertTrue(rep.truncated)
        self.assertEqual(rep.reason, "evicted")

    def test_losing_the_newest_listed_file_is_truncation(self):
        log = self.make(segment_bytes=400, cap_bytes=1 << 20)
        for i in range(1, 21):
            log.emit(upd(i))
        segs = [(s[0], s[1]) for s in log.segments()]
        after = segs[-1][0] + 1  # a cursor inside the newest file
        segs[-1][1].unlink()
        handles, lost = keeper_log._open_segments(segs, after)
        rep = keeper_log._replay(handles, after, None, None, first_seq=1,
                                 marker=None, lost_before=lost)
        self.assertEqual(handles, [])
        self.assertTrue(rep.truncated)
        self.assertGreater(rep.oldest_seq, after + 1)

    def test_a_corrupt_record_cannot_close_the_recovered_turn(self):
        log = self.make()
        log.emit(ev.turn_started(S, "t1"))
        log.emit(ev.approval_requested(S, "r1", "tool", "Run ls?"))
        path = log.segments()[-1][1]
        crash(log)
        bogus = dict(ev.turn_ended(S, "t0", "completed"), seq=0)
        with open(path, "ab") as fh:
            fh.write(json.dumps(bogus).encode() + b"\n")
            fh.write(json.dumps(dict(upd(3), seq=3)).encode() + b"\n")
        again = self.make()
        self.assertEqual(again.recovery.skipped_lines, 1)
        self.assertEqual(again.recovery.open_turn, "t1")
        self.assertEqual([p["data"]["request_id"]
                          for p in again.recovery.pending], ["r1"])

    def test_replay_rejects_a_record_below_its_files_name(self):
        log = self.make(first_seq=100)
        log.emit(upd(1))
        path = log.segments()[-1][1]
        log.close()
        # Ahead of the valid record, so the order check alone passes it.
        lines = path.read_bytes().splitlines()
        lines.insert(0, json.dumps(dict(upd(0), seq=99)).encode())
        path.write_bytes(b"\n".join(lines) + b"\n")
        rep = read_log(S, 0, home=self.home)
        self.assertEqual([e["seq"] for e in rep.events], [100])
        self.assertEqual(rep.skipped, 1)
        again = self.make()
        self.assertEqual(again.recovery.skipped_lines, 1)
        rep = again.replay(0)
        self.assertEqual([e["seq"] for e in rep.events], [100])
        self.assertEqual(rep.skipped, 1)


class TestJournalHelper(unittest.TestCase):
    """``journal._records_backward``, which recovery reuses: pinned here
    so a change in journal.py fails a keeper-log test."""

    def test_records_backward_as_recovery_uses_it(self):
        from swe.remote.journal import _records_backward
        big = "x" * (200 << 10)  # spans several 64 KiB read chunks
        lines = [
            json.dumps({"type": "turn.started", "seq": 1}),
            "not json",
            json.dumps({"type": "message.completed", "seq": 2,
                        "data": {"text": big}}),
            json.dumps({"type": "no-seq"}),
            json.dumps({"type": "approval.requested", "seq": 3}),
            "",
        ]
        path = temp_dir(self) / "events.jsonl"
        path.write_text("\n".join(lines) + "\n")
        with open(path, "rb") as fh:
            got = list(_records_backward(fh))
        self.assertEqual([r["seq"] for r in got], [3, 2, 1])  # newest first
        self.assertEqual(got[1]["data"]["text"], big)


class TestBootId(unittest.TestCase):
    def test_boot_id_is_a_string(self):
        self.assertIsInstance(keeper_log.current_boot_id(), str)

    def test_full_fsync_falls_back_to_fsync(self):
        home = temp_dir(self)
        fd = os.open(home / "f", os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        keeper_log.full_fsync(fd)
        with mock.patch.object(keeper_log.fcntl, "fcntl",
                               side_effect=OSError(45, "ENOTSUP")), \
                mock.patch.object(keeper_log.os, "fsync") as fsync:
            if getattr(keeper_log.fcntl, "F_FULLFSYNC", None) is None:
                self.skipTest("no F_FULLFSYNC here")
            keeper_log.full_fsync(fd)
        fsync.assert_called_once_with(fd)


if __name__ == "__main__":
    unittest.main()
