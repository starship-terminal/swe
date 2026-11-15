"""Tests for the status column's tail cache (``session_status_cache.json``).

What a transcript's tail says (its phase, and whether a finished turn asks
the user something) is reused while the file's stat signature holds; the
clock, live pids and bg-job states are applied fresh on every call. ``~``
is redirected to a per-test temp dir and ``now`` is passed explicitly.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.paths import session_status_cache_file_for
from swe.sessions import parse_cache, status
from swe.sessions.models import Session
from swe.sessions.status import (
    ACTIVE,
    ACTIVE_WINDOW_S,
    DONE,
    FOLLOWUP,
    INTERRUPTED,
    UNKNOWN,
    session_statuses,
)

from tests._tempdir import temp_dir

NOW = 1_700_000_000.0


def _session(tool_name, session_id, age_s=3600):
    return Session(
        timestamp=(NOW - age_s) * 1000,
        agent=tool_name,
        path="/tmp/proj",
        title="t",
        session_id=session_id,
        tool_name=tool_name,
    )


def _assistant(text):
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    }


def _tool_use():
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
            "stop_reason": "tool_use",
        },
    }


class TailCacheTest(unittest.TestCase):
    def setUp(self):
        self.home = str(temp_dir(self))
        # Files these tests just wrote count as settled; the racy rule has
        # its own tests.
        patcher = patch.dict(
            os.environ, {"HOME": self.home, "SWE_CACHE_RACY_WINDOW_MS": "0"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = session_status_cache_file_for(Path(self.home))

    def write_claude(self, sid, records):
        d = os.path.join(self.home, ".claude", "projects", "-proj")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        with open(path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def write_cursor(self, sid, records):
        d = os.path.join(
            self.home, ".cursor", "projects", "enc", "agent-transcripts", sid
        )
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        with open(path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def reads(self):
        """Count tail reads (``_tail_records`` calls) from here on."""
        calls = []
        real = status._tail_records

        def counting(path, tail_bytes):
            calls.append(path)
            return real(path, tail_bytes)

        patcher = patch.object(status, "_tail_records", side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def entries(self):
        return json.loads(self.cache.read_text())["entries"]

    def test_warm_call_matches_cold_without_reading(self):
        self.write_claude("done", [_assistant("All finished.")])
        self.write_claude("ask", [_assistant("Shall I merge it?")])
        self.write_claude("busy", [_tool_use()])
        self.write_cursor("cur", [
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "Which option do you want?"}]}},
            {"type": "turn_ended", "status": "success"},
        ])
        sessions = [
            _session("claude", "done"),
            _session("claude", "ask"),
            _session("claude", "busy"),
            _session("cursor", "cur"),
        ]
        cold = session_statuses(sessions, now=NOW)
        self.assertEqual(cold, [DONE, FOLLOWUP, INTERRUPTED, FOLLOWUP])
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        calls = self.reads()
        self.assertEqual(session_statuses(sessions, now=NOW), cold)
        self.assertEqual(calls, [])

    def test_changed_file_is_read_again(self):
        path = self.write_claude("s", [_assistant("All finished.")])
        sessions = [_session("claude", "s")]
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {
                "content": [{"type": "text", "text": "one more thing"}]}}) + "\n")
            fh.write(json.dumps(_assistant("Want me to push it?")) + "\n")
        calls = self.reads()
        self.assertEqual(session_statuses(sessions, now=NOW), [FOLLOWUP])
        self.assertEqual(calls, [path])

    def test_same_size_rewrite_with_old_mtime_is_read_again(self):
        path = self.write_claude("s", [_assistant("All finished.")])
        before = os.stat(path)
        sessions = [_session("claude", "s")]
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        # Same byte count, mtime put back: only ctime (and content) moved.
        self.write_claude("s", [_assistant("Finished all?")])
        self.assertEqual(os.path.getsize(path), before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(session_statuses(sessions, now=NOW), [FOLLOWUP])

    def test_rewrite_inside_one_coarse_clock_tick_is_read_again(self):
        # A coarse kernel clock (a few ms on Linux) or a 1 s or 2 s
        # filesystem gives two writes in the same tick one ctime and one
        # mtime. Restoring the mtime then leaves size, mtime, ctime and
        # inode all equal, so no stat can tell the rewrite apart. Model it
        # by pinning the stat's times to one fixed tick (not the live
        # clock, whose two writes could straddle a tick boundary).
        tick_ns = 2_000_000_000
        real = parse_cache._signature

        def coarse(st):
            size, _mtime, _ctime, ino = real(st)
            return [size, tick_ns, tick_ns, ino]

        patcher = patch.object(parse_cache, "_signature", coarse)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The default window, as a user has it.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_CACHE_RACY_WINDOW_MS")
        path = self.write_claude("s", [_assistant("All finished.")])
        before = os.stat(path)
        sessions = [_session("claude", "s")]
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        self.write_claude("s", [_assistant("Finished all?")])
        self.assertEqual(os.path.getsize(path), before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(session_statuses(sessions, now=NOW), [FOLLOWUP])

    def test_corrupt_entry_is_a_miss(self):
        path = self.write_claude("s", [_assistant("Shall I merge it?")])
        sessions = [_session("claude", "s")]
        session_statuses(sessions, now=NOW)
        data = json.loads(self.cache.read_text())
        sig, _value = data["entries"]["claude"][path]
        for bad in (["finished"], ["exploded", True], ["finished", "yes"], "x"):
            data["entries"]["claude"][path] = [sig, bad]
            self.cache.write_text(json.dumps(data))
            calls = self.reads()
            self.assertEqual(session_statuses(sessions, now=NOW), [FOLLOWUP])
            self.assertEqual(calls, [path])
            self.assertEqual(self.entries()["claude"][path], [sig, ["finished", True]])

    def test_corrupt_cache_file_is_ignored(self):
        self.write_claude("s", [_assistant("All finished.")])
        sessions = [_session("claude", "s")]
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("{not json")
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])

    def test_code_change_discards_entries(self):
        self.write_claude("s", [_assistant("All finished.")])
        sessions = [_session("claude", "s")]
        session_statuses(sessions, now=NOW)
        calls = self.reads()
        with patch.dict(
            parse_cache._digests, {status._TAIL_CACHE_MODULES: "1:other:beef"}
        ):
            self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        self.assertEqual(len(calls), 1)

    def test_clock_and_live_pids_apply_on_every_call(self):
        self.write_claude("s", [_tool_use()])
        self.assertEqual(
            session_statuses([_session("claude", "s", age_s=5)], now=NOW),
            [ACTIVE],
        )
        calls = self.reads()
        stale = _session("claude", "s", age_s=ACTIVE_WINDOW_S + 60)
        self.assertEqual(session_statuses([stale], now=NOW), [INTERRUPTED])
        d = os.path.join(self.home, ".claude", "sessions")
        os.makedirs(d)
        with open(os.path.join(d, "1.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "sessionId": "s"}, fh)
        self.assertEqual(session_statuses([stale], now=NOW), [ACTIVE])
        self.assertEqual(calls, [])

    def test_job_state_applies_on_every_call(self):
        self.write_claude("s", [_assistant("All finished.")])
        sessions = [_session("claude", "s")]
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])
        d = os.path.join(self.home, ".claude", "jobs", "j")
        os.makedirs(d)
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"sessionId": "s", "state": "blocked"}, fh)
        calls = self.reads()
        self.assertEqual(session_statuses(sessions, now=NOW), [FOLLOWUP])
        self.assertEqual(calls, [])

    def test_read_error_is_not_cached(self):
        path = self.write_claude("s", [_assistant("All finished.")])
        sessions = [_session("claude", "s")]
        with patch.object(status, "_tail_records", side_effect=OSError("EIO")):
            self.assertEqual(session_statuses(sessions, now=NOW), [UNKNOWN])
        self.assertNotIn(path, self.entries().get("claude", {}) if self.cache.exists() else {})
        self.assertEqual(session_statuses(sessions, now=NOW), [DONE])

    def test_subset_call_keeps_other_entries_and_gone_files_prune(self):
        a = self.write_claude("a", [_assistant("All finished.")])
        b = self.write_claude("b", [_assistant("All finished.")])
        session_statuses([_session("claude", "a"), _session("claude", "b")], now=NOW)
        # A one-row call (``swe session 1``, a remote row) leaves b alone.
        session_statuses([_session("claude", "a")], now=NOW)
        self.assertEqual(set(self.entries()["claude"]), {a, b})
        # Once b is deleted, the next rewrite drops its entry, but only
        # once the sweep interval has passed since the last sweep.
        os.remove(b)
        c = self.write_claude("c", [_assistant("All finished.")])
        session_statuses([_session("claude", "c")], now=NOW)
        self.assertEqual(set(self.entries()["claude"]), {a, b, c})
        d = self.write_claude("d", [_assistant("All finished.")])
        with patch.object(parse_cache, "_SWEEP_INTERVAL_S", 0.0):
            session_statuses([_session("claude", "d")], now=NOW)
        self.assertEqual(set(self.entries()["claude"]), {a, c, d})

    def test_busy_rewrites_do_not_stat_every_entry(self):
        # The hub's ~1 Hz probes rewrite the cache for one busy row; the
        # gone-file sweep (a stat per entry) runs once per interval.
        paths = [self.write_claude(f"s{i}", [_assistant("All finished.")]) for i in range(20)]
        session_statuses([_session("claude", f"s{i}") for i in range(20)], now=NOW)
        busy = self.write_claude("busy", [_tool_use()])
        real_exists = os.path.exists
        swept = []

        def counting(p):
            if p in paths:
                swept.append(p)
            return real_exists(p)

        with patch("swe.sessions.parse_cache.os.path.exists", side_effect=counting):
            for i in range(3):
                with open(busy, "a") as fh:
                    fh.write(json.dumps(_assistant(f"step {i}")) + "\n")
                session_statuses([_session("claude", "busy")], now=NOW)
        self.assertEqual(swept, [])
        self.assertIn(busy, self.entries()["claude"])

    def test_direct_probe_calls_are_uncached(self):
        self.write_claude("s", [_assistant("Shall I merge it?")])
        phase, text = status._probe_claude(_session("claude", "s"), {})
        self.assertEqual((phase, text), ("finished", "Shall I merge it?"))
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
