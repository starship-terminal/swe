"""The per-file parse cache behind `swe session`.

A listing must read the same whether every transcript was parsed just now or
handed back from ``~/.swe/cache/session_parse_cache.json``. The CLI tests
run the real `swe session --json` against a throwaway HOME, once cold and
once warm, and compare stdout byte for byte. The in-process tests pin HOME
with ``patch.dict`` and count how often a transcript is actually read.

Every fixture pins its mtime well in the past so no row reads as live and
the status column is stable between runs.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.paths import session_parse_cache_file_for
from swe.sessions import aggregator, failures, parse_cache
from swe.sessions.engines import jsonl_engine
from swe.sessions.parsers import parse_claude
from tests.test_json_output import decode, run_swe

from tests._tempdir import temp_dir

OLD = 1_780_000_000

CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
CODEX_UUID = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
CODEX_STEM = f"rollout-2026-09-01T10-00-00-{CODEX_UUID}"
CURSOR_ID = "c0c0c0c0-1111-2222-3333-444444444444"


def write_jsonl(path: Path, records: list, mtime: int = OLD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(rec) + "\n" for rec in records))
    os.utime(path, (mtime, mtime))


def write_json(path: Path, data, mtime: int = OLD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.utime(path, (mtime, mtime))


def claude_path(home: Path, sid: str = CLAUDE_ID) -> Path:
    return home / ".claude" / "projects" / "-work-proj" / f"{sid}.jsonl"


def add_claude(home: Path, sid: str = CLAUDE_ID, prompt: str = "fix the login bug",
               extra: tuple = (), mtime: int = OLD) -> Path:
    path = claude_path(home, sid)
    write_jsonl(path, [
        {"type": "user", "cwd": "/work/proj",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Merged. CI green."}],
            "stop_reason": "end_turn"}},
        *extra,
    ], mtime)
    return path


def add_codex(home: Path, prompt: str = "add dark mode") -> Path:
    path = home / ".codex" / "sessions" / "2026" / "09" / "01" / f"{CODEX_STEM}.jsonl"
    write_jsonl(path, [
        {"type": "session_meta", "payload": {"cwd": "/work/other"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": prompt}]}},
    ], OLD + 1)
    return path


def add_pi(home: Path) -> Path:
    path = home / ".pi" / "agent" / "sessions" / "--work-pi--" / "2026-pi.jsonl"
    write_jsonl(path, [
        {"type": "session", "cwd": "/work/pi"},
        {"type": "message", "message": {"role": "user", "content": "pi prompt"}},
        {"type": "session_info", "name": "pi named"},
    ], OLD + 2)
    return path


def add_droid(home: Path) -> Path:
    path = home / ".factory" / "sessions" / "-work-droid" / "droid-1.jsonl"
    write_jsonl(path, [
        {"type": "session_start", "cwd": "/work/droid", "title": "Droid title",
         "isSessionTitleManuallySet": True},
    ], OLD + 3)
    return path


def add_cursor_meta(home: Path, title: str) -> None:
    write_json(home / ".cursor" / "chats" / "hash" / CURSOR_ID / "meta.json",
               {"title": title, "cwd": "/work/cursor"})


def add_cursor(home: Path) -> Path:
    transcript = (home / ".cursor" / "projects" / "work-cursor" / "agent-transcripts"
                  / CURSOR_ID / f"{CURSOR_ID}.jsonl")
    write_jsonl(transcript, [
        {"role": "user", "message": {"content": [
            {"type": "text",
             "text": "<timestamp>now</timestamp><user_query>cursor prompt</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "tool_use", "input": {"path": str(home / "proj" / "a.py")}}]}},
    ], OLD + 4)
    return transcript


def freebuff_dir(home: Path) -> Path:
    return home / ".config" / "manicode" / "projects" / "fbproj" / "chats" / "fb-1"


def add_freebuff(home: Path, prompt: str = "freebuff prompt") -> Path:
    sess = freebuff_dir(home)
    write_jsonl(sess / "log.jsonl", [{"type": "log", "msg": "x"}], OLD + 5)
    write_json(sess / "run-state.json", {"sessionState": {"mainAgentState": {
        "cwd": "/work/freebuff",
        "messageHistory": [{"role": "user", "content": prompt}]}}}, OLD + 5)
    return sess / "run-state.json"


def add_everything(home: Path) -> None:
    add_claude(home)
    add_claude(home, sid="22222222-2222-3333-4444-555555555555",
               prompt="second", extra=({"type": "custom-title", "customTitle": "named"},))
    add_codex(home)
    add_pi(home)
    add_droid(home)
    add_cursor(home)
    add_freebuff(home)


class CliColdWarmTest(unittest.TestCase):
    """`swe session --json` reads the same cold, warm and after changes."""

    def setUp(self):
        self.home = temp_dir(self)
        self.cache = session_parse_cache_file_for(self.home)

    def listing(self) -> str:
        result = run_swe(
            self.home, "session", "100000", "--json",
            extra_env={"SWE_CACHE_RACY_WINDOW_MS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decode(result)
        return result.stdout

    def rows(self, stdout: str) -> dict:
        return {row["id"]: row for row in json.loads(stdout)["data"]["sessions"]}

    def test_warm_listing_is_byte_identical_to_cold(self):
        add_everything(self.home)
        self.assertFalse(self.cache.exists())
        cold = self.listing()
        self.assertTrue(self.cache.exists())
        warm = self.listing()
        self.assertEqual(cold, warm)
        self.assertEqual(len(json.loads(cold)["data"]["sessions"]), 7)

    def test_cache_file_is_owner_only(self):
        add_claude(self.home)
        self.listing()
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_appended_record_is_seen(self):
        path = add_claude(self.home)
        self.assertEqual(self.rows(self.listing())[CLAUDE_ID]["title"], "fix the login bug")
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "renamed"}) + "\n")
        os.utime(path, (OLD, OLD))  # size changes, mtime is put back
        row = self.rows(self.listing())[CLAUDE_ID]
        self.assertEqual(row["title"], "renamed")
        self.assertEqual(row["title_source"], "rename")

    def test_same_size_rewrite_with_mtime_restored_is_seen(self):
        add_claude(self.home, prompt="aaaa")
        self.assertEqual(self.rows(self.listing())[CLAUDE_ID]["title"], "aaaa")
        # Same length, same mtime: only ctime (and content) moved.
        add_claude(self.home, prompt="bbbb")
        self.assertEqual(self.rows(self.listing())[CLAUDE_ID]["title"], "bbbb")

    def test_deleted_file_drops_its_row_and_its_entry(self):
        path = add_claude(self.home)
        add_codex(self.home)
        self.listing()
        path.unlink()
        rows = self.rows(self.listing())
        self.assertNotIn(CLAUDE_ID, rows)
        self.assertIn(CODEX_STEM, rows)
        entries = json.loads(self.cache.read_text())["entries"]
        self.assertNotIn(str(path), entries.get("claude:jsonl", {}))
        self.assertTrue(entries["codex:jsonl"])

    def test_corrupt_cache_file_is_a_miss(self):
        add_everything(self.home)
        cold = self.listing()
        for garbage in (
            b"\x00\xffnot json",
            b"[]",
            json.dumps({"version": "other", "entries": {}}).encode(),
        ):
            self.cache.write_bytes(garbage)
            self.assertEqual(self.listing(), cold)
            # And it was rewritten with real entries.
            self.assertIn("claude:jsonl", json.loads(self.cache.read_text())["entries"])

    def test_malformed_entries_are_misses(self):
        add_everything(self.home)
        cold = self.listing()
        doc = json.loads(self.cache.read_text())
        for files in doc["entries"].values():
            for key in files:
                files[key] = "junk"
        doc["entries"]["pi:jsonl"] = ["not", "a", "dict"]
        self.cache.write_text(json.dumps(doc))
        self.assertEqual(self.listing(), cold)

    def test_wrong_shape_value_under_a_matching_signature_is_a_miss(self):
        add_everything(self.home)
        cold = self.listing()
        doc = json.loads(self.cache.read_text())
        for files in doc["entries"].values():
            for key, (sig, _value) in list(files.items()):
                files[key] = [sig, {"not": "the shape"}]
        self.cache.write_text(json.dumps(doc))
        self.assertEqual(self.listing(), cold)

    def test_wrong_typed_fields_under_a_matching_signature_are_a_miss(self):
        add_everything(self.home)
        cold = self.listing()
        doc = json.loads(self.cache.read_text())
        broken = {
            "claude:jsonl": lambda v: [v[0], 123, v[2]],
            "claude:handoff": lambda v: 7,
            "cursor:scan": lambda v: [v[0], [1, 2]],
            "freebuff:run-state": lambda v: [v[0], 5],
        }
        for namespace, damage in broken.items():
            for key, (sig, value) in list(doc["entries"][namespace].items()):
                doc["entries"][namespace][key] = [sig, damage(value)]
        self.cache.write_text(json.dumps(doc))
        self.assertEqual(self.listing(), cold)

    def test_poisoned_entry_with_a_stale_signature_is_ignored(self):
        path = add_claude(self.home)
        self.listing()
        doc = json.loads(self.cache.read_text())
        sig, _value = doc["entries"]["claude:jsonl"][str(path)]
        doc["entries"]["claude:jsonl"][str(path)] = [
            [sig[0] + 1, *sig[1:]], ["/elsewhere", "poisoned", "rename"]]
        self.cache.write_text(json.dumps(doc))
        row = self.rows(self.listing())[CLAUDE_ID]
        self.assertEqual(row["title"], "fix the login bug")
        self.assertEqual(row["cwd"], "/work/proj")

    def test_codex_index_rename_is_seen_while_the_rollout_is_cached(self):
        add_codex(self.home)
        self.assertEqual(self.rows(self.listing())[CODEX_STEM]["title"], "add dark mode")
        index = self.home / ".codex" / "session_index.jsonl"
        index.write_text(json.dumps({
            "id": CODEX_UUID, "thread_name": "dark mode v2",
            "updated_at": "2026-09-01T10:00:01Z"}) + "\n")
        self.assertEqual(self.rows(self.listing())[CODEX_STEM]["title"], "dark mode v2")

    def test_cursor_meta_title_is_seen_while_the_transcript_is_cached(self):
        add_cursor(self.home)
        self.assertEqual(self.rows(self.listing())[CURSOR_ID]["title"], "cursor prompt")
        add_cursor_meta(self.home, "Cursor named it")
        row = self.rows(self.listing())[CURSOR_ID]
        self.assertEqual(row["title"], "Cursor named it")
        self.assertEqual(row["cwd"], "/work/cursor")

    def test_freebuff_run_state_change_is_seen_while_the_log_is_cached(self):
        state = add_freebuff(self.home)
        self.assertEqual(self.rows(self.listing())["fb-1"]["title"], "freebuff prompt")
        write_json(state, {"sessionState": {"mainAgentState": {
            "cwd": "/work/moved",
            "messageHistory": [{"role": "user", "content": "a longer new prompt"}]}}},
            OLD + 5)
        row = self.rows(self.listing())["fb-1"]
        self.assertEqual(row["title"], "a longer new prompt")
        self.assertEqual(row["cwd"], "/work/moved")

    def test_claude_handoff_successor_appearing_later_is_seen(self):
        successor = "33333333-2222-3333-4444-555555555555"
        add_claude(self.home, extra=(
            {"type": "continued-in", "continuedInSessionId": successor},))
        self.assertIn(CLAUDE_ID, self.rows(self.listing()))
        add_claude(self.home, sid=successor, prompt="carry on", mtime=OLD + 10)
        rows = self.rows(self.listing())
        self.assertNotIn(CLAUDE_ID, rows)
        self.assertIn(successor, rows)


class InProcessCacheTest(unittest.TestCase):
    """Counts reads and checks scoping, with HOME pinned in-process."""

    def setUp(self):
        self.home = temp_dir(self)
        # Files these tests just wrote count as settled; the racy rule has
        # its own tests below.
        patcher = patch.dict(
            os.environ,
            {"HOME": str(self.home), "SWE_CACHE_RACY_WINDOW_MS": "0"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        failures.clear()
        self.addCleanup(failures.clear)

    def scans(self):
        """Patch the per-file scan with a counting wrapper."""
        calls = []
        real = jsonl_engine._scan_jsonl

        def counting(fp, config):
            calls.append(fp)
            return real(fp, config)

        patcher = patch.object(jsonl_engine, "_scan_jsonl", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def listing(self, **kw):
        return [
            (s.timestamp, s.agent, s.path, s.title, s.session_id, s.tool_name,
             s.title_source)
            for s in aggregator.get_all_sessions(limit=None, **kw)
        ]

    def test_uncached_parsers_match_cold_and_warm_listings(self):
        add_everything(self.home)
        # Outside activate() nothing is cached: this is the pre-cache path.
        reference = []
        for _name, parser, _keys in aggregator.PARSER_REGISTRY:
            reference.extend(parser())
        reference.sort(key=lambda s: s.timestamp, reverse=True)
        reference = [
            (s.timestamp, s.agent, s.path, s.title, s.session_id, s.tool_name,
             s.title_source)
            for s in reference
        ]
        self.assertEqual(self.listing(), reference)
        self.assertEqual(self.listing(), reference)

    def test_warm_listing_reads_no_unchanged_transcript(self):
        add_everything(self.home)
        calls = self.scans()
        self.listing()
        cold = len(calls)
        self.assertGreater(cold, 0)
        self.listing()
        self.assertEqual(len(calls), cold)

    def test_only_the_changed_file_is_read_again(self):
        add_everything(self.home)
        changed = add_pi(self.home)
        calls = self.scans()
        self.listing()
        calls.clear()
        with open(changed, "a") as fh:
            fh.write("\n")
        self.listing()
        self.assertEqual(calls, [str(changed)])

    def test_parsers_outside_a_listing_do_not_touch_the_cache(self):
        add_claude(self.home)
        parse_claude()
        self.assertFalse(session_parse_cache_file_for(self.home).exists())

    def test_agent_filtered_listing_keeps_other_harnesses_entries(self):
        add_claude(self.home)
        add_codex(self.home)
        self.listing()
        self.listing(agent="cc")
        entries = json.loads(session_parse_cache_file_for(self.home).read_text())["entries"]
        self.assertTrue(entries["claude:jsonl"])
        self.assertTrue(entries["codex:jsonl"])

    def test_unchanged_warm_listing_does_not_rewrite_the_file(self):
        add_everything(self.home)
        self.listing()
        cache = session_parse_cache_file_for(self.home)
        before = cache.stat().st_mtime_ns
        os.utime(cache, ns=(before - 10_000_000_000, before - 10_000_000_000))
        self.listing()
        self.assertEqual(cache.stat().st_mtime_ns, before - 10_000_000_000)

    def test_unwritable_cache_dir_still_lists(self):
        add_claude(self.home)
        (self.home / ".swe").mkdir()
        (self.home / ".swe" / "cache").write_text("a file, not a dir")
        self.assertEqual(len(self.listing()), 1)
        self.assertEqual(len(self.listing()), 1)

    def test_a_failed_read_is_not_cached(self):
        path = add_claude(self.home)
        real_open = open

        def failing_open(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError(13, "transient", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            rows = self.listing()
        self.assertEqual([r[3] for r in rows], [""])  # nothing could be read
        # Nothing was storable, so nothing was written.
        self.assertFalse(session_parse_cache_file_for(self.home).exists())
        # Reads recover; the file itself never changed.
        self.assertEqual([r[3] for r in self.listing()], ["fix the login bug"])

    def test_a_failed_sidecar_read_is_not_cached(self):
        state = add_freebuff(self.home)
        real_open = open

        def failing_open(file, *args, **kwargs):
            if str(file) == str(state):
                raise OSError(5, "I/O error", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            self.assertEqual([r[3] for r in self.listing()], [""])
        self.assertEqual([r[3] for r in self.listing()], ["freebuff prompt"])

    def test_malformed_file_content_is_cached(self):
        # Bad JSON is a property of the bytes, not of the attempt.
        path = claude_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("not json\n" + json.dumps(
            {"type": "user", "cwd": "/work/proj",
             "message": {"role": "user", "content": "after junk"}}) + "\n")
        calls = self.scans()
        self.listing()
        self.listing()
        self.assertEqual(len(calls), 1)

    def test_code_change_discards_entries(self):
        add_claude(self.home)
        calls = self.scans()
        self.listing()
        calls.clear()
        with patch.object(parse_cache, "_fingerprint", "1:other:deadbeef"):
            self.listing()
        self.assertEqual(len(calls), 1)

    def test_activate_is_reentrant_and_saves_once(self):
        add_claude(self.home)
        with patch.object(parse_cache._Store, "save", autospec=True) as save:
            with parse_cache.activate() as outer:
                with parse_cache.activate() as inner:
                    self.assertIs(inner, outer)
                    parse_claude()
                self.assertEqual(save.call_count, 0)
            self.assertEqual(save.call_count, 1)

    def test_tuple_values_come_back_as_lists_hit_or_miss(self):
        path = add_claude(self.home)
        with parse_cache.activate():
            miss = parse_cache.lookup("t:x", str(path), lambda: ("a", 1))
        with parse_cache.activate():
            hit = parse_cache.lookup("t:x", str(path), lambda: ("b", 2))
        self.assertEqual(miss, ["a", 1])
        self.assertEqual(hit, ["a", 1])

    def test_missing_file_computes_without_caching(self):
        missing = str(self.home / "nope.jsonl")
        with parse_cache.activate() as store:
            self.assertEqual(parse_cache.lookup("t:x", missing, lambda: "x"), "x")
            self.assertEqual(store.entries.get("t:x", {}), {})


class RacyWindowTest(unittest.TestCase):
    """A value is stored only once the file's ctime is older than the window."""

    def setUp(self):
        self.dir = temp_dir(self)
        self.cache = self.dir / "cache.json"
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_CACHE_RACY_WINDOW_MS", None)
        self.path = self.dir / "t.jsonl"
        self.path.write_text("x")
        self.calls = 0

    def look(self, now_ns=None):
        def compute():
            self.calls += 1
            return [self.calls]

        with patch.object(parse_cache.time, "time_ns",
                          return_value=now_ns or parse_cache.time.time_ns()):
            with parse_cache.standalone(lambda: self.cache, ("swe.sessions.parse_cache",)) as store:
                return parse_cache.lookup_in(store, "t:x", str(self.path), compute)

    def test_a_file_written_just_now_is_read_every_time(self):
        self.assertEqual(self.look(), [1])
        self.assertEqual(self.look(), [2])
        self.assertFalse(self.cache.exists())

    def test_a_file_older_than_the_window_is_stored_and_reused(self):
        later = self.path.stat().st_ctime_ns + 3_000_000_000
        self.assertEqual(self.look(later), [1])
        self.assertEqual(self.look(later), [1])

    def test_a_ctime_in_the_future_counts_as_fresh(self):
        earlier = self.path.stat().st_ctime_ns - 10_000_000_000
        self.assertEqual(self.look(earlier), [1])
        self.assertEqual(self.look(earlier), [2])

    def test_a_racy_read_drops_the_entry_it_replaces(self):
        # Under a coarse clock tick the rewrite keeps the stored signature,
        # so only the freshness check can tell: model that, not nanoseconds.
        real = parse_cache._signature
        patcher = patch.object(
            parse_cache, "_signature",
            lambda st: [real(st)[0], 0, 0, real(st)[3]],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        later = self.path.stat().st_ctime_ns + 3_000_000_000
        self.look(later)
        self.assertTrue(self.cache.exists())
        self.path.write_text("y")
        self.look()  # fresh again: read, and not remembered
        self.assertEqual(self.look(), [3])
        self.assertNotIn(str(self.path), json.loads(self.cache.read_text())["entries"].get("t:x", {}))


if __name__ == "__main__":
    unittest.main()
