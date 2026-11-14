"""The hook backfill's parse cache (``hooks/transcript_cache.py``).

A transcript is parsed once and read back while its size, mtime, ctime
and inode stay the same; what a command runs is worked out on every read,
so a change on disk shows even when the transcript did not change.
"""

import json
import os
import sqlite3
import stat
import time
import unittest
from pathlib import Path
from unittest import mock

from swe.hooks import transcript_cache, transcripts
from swe.hooks.identity import Resolver

from tests._tempdir import temp_dir
from tests.test_hooks_stats import _rec

HOUR = 3600


def _db(path):
    """A connection the test closes when it ends."""
    return sqlite3.connect(path, isolation_level=None)


class TranscriptCacheTest(unittest.TestCase):
    def connect(self, path):
        db = _db(path)
        self.addCleanup(db.close)
        return db

    def setUp(self):
        self.home = temp_dir(self)
        (self.home / ".claude").mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_HOOK_CACHE", None)
        self.now = time.time() - 600
        project = self.home / ".claude" / "projects" / "-w-app"
        project.mkdir(parents=True)
        self.path = project / "s.jsonl"
        self.command = f'python3 "{self.home}/.claude/hooks/_generated/notify.py"'
        self.write(3)

    def write(self, runs: int) -> None:
        self.path.write_text("\n".join(
            _rec(self.now - i * HOUR, "attachment", attachment={
                "type": "hook_success", "hookName": "Stop", "hookEvent": "Stop",
                "command": self.command, "exitCode": 0, "durationMs": 5,
                "stderr": "", "stdout": ""})
            for i in range(runs)) + "\n")

    def read(self):
        return transcripts.backfill(self.home, self.now - 30 * 86400)

    def parses(self):
        """How many files the next backfill parses."""
        spy = mock.patch.object(transcripts, "_raw_file",
                                wraps=transcripts._raw_file)
        return spy

    def test_a_second_read_parses_nothing_and_answers_the_same(self):
        with self.parses() as spy:
            first = self.read()
        self.assertEqual(spy.call_count, 1)
        with self.parses() as spy:
            second = self.read()
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(first, second)
        self.assertEqual(len(second), 3)

    def test_a_transcript_with_no_hook_runs_is_cached_too(self):
        quiet = self.path.with_name("quiet.jsonl")
        quiet.write_text(_rec(self.now, "user", message={
            "role": "user", "content": "no hooks here"}) + "\n")
        self.read()
        with self.parses() as spy:
            self.read()
        self.assertEqual(spy.call_count, 0)

    def test_a_changed_transcript_is_parsed_again(self):
        self.read()
        self.write(5)
        with self.parses() as spy:
            self.assertEqual(len(self.read()), 5)
        self.assertEqual(spy.call_count, 1)

    def test_what_a_command_runs_is_read_fresh_on_a_hit(self):
        # Nothing at the wrapper path and no shared script: the run is
        # the missing path's.
        before = {e["script"] for e in self.read()}
        # The shared script appears: the cached transcript is now its run.
        shared = self.home / ".swe" / "hooks" / "shared" / "notify.py"
        shared.parent.mkdir(parents=True)
        shared.write_text("print(1)\n")
        with self.parses() as spy:
            after = {e["script"] for e in self.read()}
        self.assertEqual(spy.call_count, 0)
        self.assertNotEqual(before, after)
        self.assertEqual(after, {os.path.realpath(shared)})

    def test_a_new_parser_version_reads_nothing_old(self):
        self.read()
        with mock.patch.object(transcript_cache, "_version", lambda: "next"), \
                self.parses() as spy:
            self.read()
        self.assertEqual(spy.call_count, 1)

    def test_the_switch_turns_it_off(self):
        with mock.patch.dict(os.environ, {"SWE_HOOK_CACHE": "0"}):
            self.read()
            with self.parses() as spy:
                self.read()
        self.assertEqual(spy.call_count, 1)
        self.assertFalse(transcript_cache.cache_file_for(self.home).exists())

    def test_a_broken_store_reads_as_a_miss(self):
        db = transcript_cache.cache_file_for(self.home)
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a database" * 100)
        self.assertEqual(len(self.read()), 3)
        self.assertEqual(len(self.read()), 3)

    def test_the_store_is_private(self):
        self.read()
        db = transcript_cache.cache_file_for(self.home)
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(db.parent.stat().st_mode), 0o700)

    def test_the_store_is_trimmed_to_its_cap_least_recent_first(self):
        old = self.path.with_name("old.jsonl")
        old.write_text(self.path.read_text())
        self.read()
        db = self.connect(transcript_cache.cache_file_for(self.home))
        db.execute("UPDATE files SET used = 1 WHERE path LIKE '%/old.jsonl'")
        db.execute("UPDATE files SET used = 2 WHERE path LIKE '%/s.jsonl'")
        (size,) = db.execute(
            "SELECT bytes FROM files WHERE path LIKE '%/s.jsonl'").fetchone()
        # Room for one row: the one read longest ago goes.
        with mock.patch.object(transcript_cache, "CAP_BYTES", size):
            transcript_cache.TranscriptCache._trim(db)
        rows = [Path(p).name for (p,) in db.execute("SELECT path FROM files")]
        self.assertEqual(rows, ["s.jsonl"])
        # A read that writes trims too.
        with mock.patch.object(transcript_cache, "CAP_BYTES", 1):
            self.write(4)
            self.read()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_two_readers_at_once_do_not_break_each_other(self):
        a = transcript_cache.TranscriptCache(self.home)
        b = transcript_cache.TranscriptCache(self.home)
        parse = transcripts._raw_file
        self.assertEqual(len(a.events(str(self.path), parse)), 3)
        self.assertEqual(len(b.events(str(self.path), parse)), 3)
        a.close()
        b.close()
        c = transcript_cache.TranscriptCache(self.home)
        with self.parses() as spy:
            self.assertEqual(len(c.events(str(self.path), transcripts._raw_file)), 3)
        c.close()
        self.assertEqual(spy.call_count, 0)

    def test_the_cached_rows_hold_only_what_the_file_says(self):
        self.read()
        db = self.connect(transcript_cache.cache_file_for(self.home))
        (blob,) = db.execute("SELECT events FROM files").fetchone()
        rows = transcript_cache._unpack(blob)
        for row in rows:
            # No name, no script, no repository: those are read fresh.
            self.assertIsNone(row["hook"])
            self.assertIsNone(row["script"])
            self.assertIsNone(row["repo"])
            self.assertEqual(row["_command"], self.command)

    def test_corrupt_or_truncated_rows_read_as_a_miss(self):
        self.read()
        db = self.connect(transcript_cache.cache_file_for(self.home))
        (good,) = db.execute("SELECT events FROM files").fetchone()
        import zlib
        bad_rows = [
            good[: len(good) // 2],                          # truncated
            b"\xff" * 64,                                    # not zlib
            zlib.compress(b"{not json"),                      # not JSON
            zlib.compress(b'{"n": 2, "k": ["ts"], "c": [{"v": [1]}]}'),  # short
            zlib.compress(b'{"n": 1, "k": ["ts"], "c": [{"v": ["noon"]}]}'),  # type
            zlib.compress(b'{"n": 2, "k": ["ts", "x"], "c": [{"c": 1}, '
                          b'{"d": ["a"], "i": [0, -1]}]}'),   # bad index
            zlib.compress(b'{"n": 1, "k": ["ts"], "c": [{"c": 1}, {"c": 2}]}'),
            zlib.compress(b"[1, 2, 3]"),                      # wrong shape
            zlib.compress(b"\xe3\x01\x00\x00\x00" * 50),     # marshal-ish
        ]
        for blob in bad_rows:
            db.execute("UPDATE files SET events = ?", (blob,))
            with self.parses() as spy:
                self.assertEqual(len(self.read()), 3, blob[:12])
            self.assertEqual(spy.call_count, 1, blob[:12])

    def big_library(self):
        import random
        rng = random.Random(7)
        project = self.path.parent
        noise = lambda: "".join(rng.choice("abcdefghijklmnop") for _ in range(180))
        for n in range(60):
            (project / f"big-{n}.jsonl").write_text("\n".join(
                _rec(self.now - i, "attachment", attachment={
                    "type": "hook_success", "hookName": "Stop",
                    "hookEvent": "Stop", "command": f"python3 /x/{noise()}.py",
                    "exitCode": 0, "durationMs": 5, "stderr": noise(),
                    "stdout": ""}) for i in range(200)) + "\n")

    def store_size(self) -> int:
        db = transcript_cache.cache_file_for(self.home)
        return sum(p.stat().st_size for p in db.parent.glob(db.name + "*"))

    def test_freed_pages_go_back_to_the_disk(self):
        # One batch far over the cap: everything is written, then trimmed,
        # and the trimmed pages must leave the file.
        self.big_library()
        cap = 256 * 1024
        with mock.patch.object(transcript_cache, "CAP_BYTES", cap), \
                mock.patch.object(transcript_cache, "BATCH_BYTES", 1 << 40):
            self.read()
        self.assertLess(self.store_size(), cap + 128 * 1024)

    def test_a_store_made_without_auto_vacuum_is_rebuilt_with_it(self):
        db = transcript_cache.cache_file_for(self.home)
        db.parent.mkdir(parents=True)
        old = self.connect(db)
        old.execute(transcript_cache._SCHEMA)
        self.assertEqual(old.execute("PRAGMA auto_vacuum").fetchone()[0], 0)
        old.close()
        self.read()
        self.assertEqual(self.connect(db).execute(
            "PRAGMA auto_vacuum").fetchone()[0], 2)

    def test_the_file_comes_back_under_its_bound_after_a_big_read(self):
        self.big_library()
        cap, batch = 256 * 1024, 128 * 1024
        with mock.patch.object(transcript_cache, "CAP_BYTES", cap), \
                mock.patch.object(transcript_cache, "BATCH_BYTES", batch):
            self.read()
        db = transcript_cache.cache_file_for(self.home)
        rows = self.connect(db).execute(
            "SELECT SUM(bytes), COUNT(*) FROM files").fetchone()
        written = sum(len(transcript_cache._pack(transcripts._raw_file(str(p))))
                      for p in self.path.parent.glob("*.jsonl"))
        size = sum(p.stat().st_size for p in db.parent.glob(db.name + "*"))
        # The parses were several times the cap; the file (with its log) is
        # back under the cap plus one write batch plus SQLite's own pages.
        self.assertGreater(written, 4 * cap)
        self.assertLess(size, cap + batch + 128 * 1024)
        self.assertLessEqual(rows[0], cap)
        self.assertGreater(rows[1], 0)
        rows = self.connect(db).execute("SELECT SUM(bytes) FROM files")
        self.assertLessEqual(rows.fetchone()[0], cap)

    def test_the_fingerprint_names_the_modules_that_decide_a_row(self):
        self.assertEqual(set(transcript_cache._FINGERPRINT_MODULES), {
            "swe.hooks.transcript_cache", "swe.hooks.transcripts",
            "swe.hooks.events", "swe.hooks.shim_runtime"})


if __name__ == "__main__":
    unittest.main()
