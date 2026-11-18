"""ledger/parsers.py and models.py: the Swift ledger's line rules."""

import json
import unittest
from pathlib import Path

from swe.ledger import parsers
from swe.ledger.models import MAX_TOKENS, parse_timestamp

from tests._tempdir import temp_dir


def _claude(**over):
    usage = over.pop("usage", {"input_tokens": 10, "output_tokens": 20,
                               "cache_read_input_tokens": 300,
                               "cache_creation_input_tokens": 40})
    line = {
        "type": "assistant", "timestamp": "2026-10-03T10:00:03.123Z",
        "sessionId": "s1", "requestId": "req1", "cwd": "/w", "version": "2.1.0",
        "message": {"id": "msg1", "model": "claude-opus-5", "usage": usage},
    }
    line.update(over)
    return json.dumps(line).encode() + b"\n"


class TestTimestamp(unittest.TestCase):
    def test_utc_fast_path_any_fraction(self):
        self.assertEqual(parse_timestamp("1970-01-01T00:00:01Z"), 1.0)
        self.assertAlmostEqual(parse_timestamp("1970-01-01T00:00:01.123456789Z"), 1.123456789)

    def test_offset_and_garbage(self):
        self.assertEqual(parse_timestamp("1970-01-01T08:00:00+08:00"), 0.0)
        self.assertIsNone(parse_timestamp("yesterday"))
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp("2026-10-03T10:00:03"))  # no zone

    def test_impossible_dates_are_unreadable(self):
        self.assertIsNone(parse_timestamp("2026-02-30T10:00:00Z"))
        self.assertIsNone(parse_timestamp("2026-04-31T10:00:00Z"))
        self.assertIsNone(parse_timestamp("2026-02-29T10:00:00Z"))  # not a leap year
        self.assertIsNotNone(parse_timestamp("2028-02-29T10:00:00Z"))
        self.assertIsNotNone(parse_timestamp("2000-02-29T10:00:00Z"))
        self.assertIsNone(parse_timestamp("2100-02-29T10:00:00Z"))


class TestClaude(unittest.TestCase):
    def test_key_and_tokens(self):
        [rec] = parsers.claude_records(_claude(), "/h")
        self.assertEqual(rec.key, "claude:msg1:req1")
        self.assertEqual(rec.msg, "msg1")
        e = rec.event
        self.assertEqual((e.input, e.output, e.cache_read, e.cache_write), (10, 20, 300, 40))
        self.assertEqual(e.total, 370)
        self.assertEqual((e.session, e.cwd, e.model, e.account), ("s1", "/w", "claude-opus-5", "/h"))

    def test_absurd_counts_are_capped(self):
        """A corrupt count (1e19) is capped, so the event's sums and the
        store's 64-bit integers can't overflow and abort the scan."""
        usage = {"input_tokens": 1e19, "output_tokens": 5,
                 "cache_read_input_tokens": 2 ** 70}
        [rec] = parsers.claude_records(_claude(usage=usage), "/h")
        self.assertEqual(rec.event.input, MAX_TOKENS)
        self.assertEqual(rec.event.cache_read, MAX_TOKENS)
        self.assertEqual(rec.event.output, 5)

    def test_key_without_request_or_message_id(self):
        line = json.loads(_claude())
        del line["requestId"]
        [rec] = parsers.claude_records(json.dumps(line).encode(), "/h")
        self.assertEqual(rec.key, "claude:msg1")
        del line["message"]["id"]
        [rec] = parsers.claude_records(json.dumps(line).encode(), "/h")
        self.assertEqual(rec.key, "claude:anon:s1:2026-10-03T10:00:03.123Z:claude-opus-5")
        self.assertIsNone(rec.msg)

    def test_thinking_comes_out_of_output(self):
        [rec] = parsers.claude_records(_claude(usage={
            "input_tokens": 1, "output_tokens": 100,
            "output_tokens_details": {"thinking_tokens": 30}}), "/h")
        self.assertEqual((rec.event.output, rec.event.reasoning), (70, 30))

    def test_cache_creation_split(self):
        [rec] = parsers.claude_records(_claude(usage={
            "input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 50,
            "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 40}}), "/h")
        self.assertEqual((rec.event.cache_write, rec.event.cache_write_1h), (60, 40))

    def test_rejected_lines(self):
        self.assertEqual(parsers.claude_records(b'{"type": "user"}\n', "/h"), [])
        self.assertEqual(parsers.claude_records(_claude(version="abc"), "/h"), [])
        self.assertEqual(parsers.claude_records(_claude(requestId=""), "/h"), [])
        self.assertEqual(parsers.claude_records(_claude(timestamp="nope"), "/h"), [])
        self.assertEqual(parsers.claude_records(_claude(usage={"input_tokens": 0, "output_tokens": 0}), "/h"), [])

    def test_logged_cost_fast_tier_sidechain(self):
        line = json.loads(_claude(costUSD=1.5, isSidechain=True))
        line["message"]["usage"]["speed"] = "fast"
        [rec] = parsers.claude_records(json.dumps(line).encode(), "/h")
        self.assertEqual((rec.event.cost, rec.event.tier, rec.sidechain), (1.5, "fast", True))

    def test_advisor_iterations(self):
        usage = {"input_tokens": 1, "output_tokens": 1, "iterations": [
            {"type": "message", "input_tokens": 5, "output_tokens": 5},
            {"type": "advisor_message", "model": "claude-fable-5", "input_tokens": 7, "output_tokens": 3},
        ]}
        recs = parsers.claude_records(_claude(usage=usage), "/h")
        self.assertEqual([r.key for r in recs], ["claude:msg1:req1", "claude:msg1:req1:advisor:0"])
        self.assertEqual(recs[1].event.model, "claude-fable-5")
        self.assertEqual(recs[1].msg, "msg1:advisor:0")


def _codex(kind, payload, ts="2026-10-03T10:00:00.000Z"):
    return {"timestamp": ts, "type": kind, "payload": payload}


def _tc(total, last=None, ts="2026-10-03T10:00:00.000Z"):
    info = {"total_token_usage": total}
    if last is not None:
        info["last_token_usage"] = last
    return _codex("event_msg", {"type": "token_count", "info": info}, ts)


class TestCodex(unittest.TestCase):
    def setUp(self):
        self.p = parsers.CodexFileParser(Path("/x/rollout-abc.jsonl"), "/c")

    def test_cumulative_totals_become_deltas_and_repeats_are_dropped(self):
        self.p.records(_codex("turn_context", {"model": "gpt-5.5", "cwd": "/w"}))
        t1 = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10,
              "reasoning_output_tokens": 4, "total_tokens": 110}
        t2 = {"input_tokens": 150, "cached_input_tokens": 80, "output_tokens": 30,
              "reasoning_output_tokens": 4, "total_tokens": 180}
        [a] = self.p.records(_tc(t1, ts="2026-10-03T10:00:01Z"))
        self.assertEqual(a.key, "codex:rollout-abc:2026-10-03T10:00:01Z")
        self.assertEqual((a.event.input, a.event.cache_read, a.event.output, a.event.reasoning),
                         (40, 60, 6, 4))
        self.assertEqual((a.event.model, a.event.cwd, a.event.session), ("gpt-5.5", "/w", "rollout-abc"))
        [b] = self.p.records(_tc(t2, ts="2026-10-03T10:00:02Z"))
        self.assertEqual((b.event.input, b.event.cache_read, b.event.output), (30, 20, 20))
        # A replayed total emits nothing.
        self.assertEqual(self.p.records(_tc(t1, ts="2026-10-03T10:00:03Z")), [])

    def test_last_usage_wins_over_delta(self):
        total = {"input_tokens": 500, "output_tokens": 50}
        [r] = self.p.records(_tc(total, last={"input_tokens": 9, "output_tokens": 1}))
        self.assertEqual((r.event.input, r.event.output, r.event.model), (9, 1, "gpt-5"))

    def test_child_waits_for_its_own_turn(self):
        self.p.records(_codex("session_meta", {"forked_from_id": "parent", "timestamp": "2026-10-03T10:00:00Z"}))
        self.assertEqual(self.p.records(_tc({"input_tokens": 5, "output_tokens": 5})), [])
        self.p.records(_codex("event_msg", {"type": "task_started", "started_at": parse_timestamp("2026-10-03T10:00:30Z")}))
        [r] = self.p.records(_tc({"input_tokens": 8, "output_tokens": 6}, ts="2026-10-03T10:01:00Z"))
        self.assertEqual(r.event.input, 3)

    def test_records_mode_ignores_token_counts(self):
        [r] = self.p.records(_codex("token_usage_record", {
            "response_id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 2}}))
        self.assertEqual(r.key, "codex:resp:resp_1")
        self.assertEqual(self.p.records(_tc({"input_tokens": 99, "output_tokens": 9})), [])

    def test_a_malformed_record_does_not_suppress_token_counts(self):
        # No `usage`: the line can't count, so token_count lines stay live.
        self.assertEqual(self.p.records(_codex("token_usage_record", {
            "response_id": "resp_1"})), [])
        [a] = self.p.records(_tc({"input_tokens": 7, "output_tokens": 3},
                                 ts="2026-10-03T10:00:01Z"))
        self.assertEqual(a.event.input, 7)
        # An unparseable timestamp is the same.
        self.assertEqual(self.p.records(
            _codex("token_usage_record",
                   {"usage": {"input_tokens": 1, "output_tokens": 1}},
                   ts="not-a-time")), [])
        [b] = self.p.records(_tc({"input_tokens": 12, "output_tokens": 4},
                                 ts="2026-10-03T10:00:02Z"))
        self.assertEqual(b.event.input, 5)
        # A record that does count still flips the file into record mode.
        self.assertEqual(len(self.p.records(_codex("token_usage_record", {
            "response_id": "resp_2", "usage": {"input_tokens": 1, "output_tokens": 1}}))), 1)
        self.assertEqual(self.p.records(_tc({"input_tokens": 50, "output_tokens": 8})), [])

    def test_service_tier(self):
        self.p.records(_codex("event_msg", {"type": "thread_settings_applied",
                                            "thread_settings": {"service_tier": "fast"}}))
        [r] = self.p.records(_tc({"input_tokens": 1, "output_tokens": 1}))
        self.assertEqual(r.event.tier, "priority")
        self.assertIsNone(parsers.normalized_tier("Default"))

    def test_file_reader_skips_partial_last_line(self):
        d = temp_dir(self)
        f = d / "rollout-z.jsonl"
        lines = [_codex("turn_context", {"model": "gpt-5.5"}),
                 _tc({"input_tokens": 3, "output_tokens": 1})]
        f.write_text("\n".join(json.dumps(x) for x in lines) + "\n" + '{"type": "event_msg"')
        recs = parsers.codex_file_records(f, "/c")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].event.model, "gpt-5.5")


class TestClaudeEdges(unittest.TestCase):
    def test_lines_that_mention_usage_but_carry_none(self):
        for line in (b'{"usage": \n', b'["usage"]\n', b'{"usage": 1, "message": "text"}\n',
                     b'{"message": {"usage": [1]}}\n',
                     _claude(usage={"input_tokens": "10", "output_tokens": 20}),
                     _claude(usage={"input_tokens": 10})):
            self.assertEqual(parsers.claude_records(line, "/h"), [], line)

    def test_advisor_without_a_model_or_tokens_adds_nothing(self):
        usage = {"input_tokens": 1, "output_tokens": 1, "iterations": [
            "noise",
            {"type": "advisor_message", "input_tokens": 9, "output_tokens": 9},
            {"type": "advisor_message", "model": "claude-fable-5", "input_tokens": 0, "output_tokens": 0},
            {"type": "advisor_message", "model": "claude-fable-5", "input_tokens": 2, "output_tokens": 1},
        ]}
        recs = parsers.claude_records(_claude(usage=usage), "/h")
        # The zero-token advisor still takes index 0, as the Swift reader counts.
        self.assertEqual([r.key for r in recs], ["claude:msg1:req1", "claude:msg1:req1:advisor:1"])
        line = json.loads(_claude(usage=usage))
        del line["message"]["id"]
        recs = parsers.claude_records(json.dumps(line).encode(), "/h")
        self.assertEqual([r.msg for r in recs], [None, None])


class TestCodexEdges(unittest.TestCase):
    def setUp(self):
        self.p = parsers.CodexFileParser(Path("/x/rollout-abc.jsonl"), "/c")

    def test_lines_that_say_nothing(self):
        for obj in ({"type": "session_meta", "payload": "not a dict"},
                    _codex("response_item", {"type": "message"}),
                    _codex("event_msg", {"type": "agent_message"}),
                    _codex("event_msg", {"type": "token_count", "info": None}),
                    _codex("event_msg", {"type": "token_count", "info": {}}),
                    _tc({"input_tokens": 0, "output_tokens": 0}),
                    _codex("turn_context", {"model": "", "cwd": None})):
            self.assertEqual(self.p.records(obj), [], obj)
        self.assertIsNone(self.p.model)
        self.assertEqual(self.p.records(_tc({"input_tokens": 1, "output_tokens": 1}, ts=None)), [])

    def test_only_the_first_session_meta_counts(self):
        self.p.records(_codex("session_meta", {"cwd": "/first"}))
        self.p.records(_codex("session_meta", {"cwd": "/second", "forked_from_id": "p"}))
        self.assertEqual((self.p.cwd, self.p.gate), ("/first", None))

    def test_last_usage_alone_counts_without_a_total(self):
        info = {"last_token_usage": {"input_tokens": 4, "output_tokens": 2}}
        [r] = self.p.records(_codex("event_msg", {"type": "token_count", "info": info}))
        self.assertEqual((r.event.input, r.event.output), (4, 2))

    def test_child_markers_and_gate_from_the_line_time(self):
        for payload in ({"thread_source": "subagent"}, {"source": {"subagent": {"name": "x"}}},
                        {"parent_thread_id": "p"}):
            self.assertTrue(parsers.CodexFileParser._is_child(payload), payload)
        self.assertFalse(parsers.CodexFileParser._is_child({"source": "cli"}))
        # No creation stamp: the meta line's own time is the gate.
        self.p.records(_codex("session_meta", {"thread_source": "subagent"}, ts="2026-10-03T10:00:10Z"))
        self.assertEqual(self.p.gate, parse_timestamp("2026-10-03T10:00:10Z"))
        self.p.records(_codex("event_msg", {"type": "task_started",
                                            "started_at": parse_timestamp("2026-10-03T10:00:05Z")}))
        self.assertIsNotNone(self.p.gate)  # a replayed turn from before the fork
        self.p.records(_codex("event_msg", {"type": "task_started", "started_at": True}))
        self.assertIsNotNone(self.p.gate)
        self.p.records(_codex("event_msg", {"type": "task_started",
                                            "started_at": parse_timestamp("2026-10-03T10:00:10Z")}))
        self.assertIsNone(self.p.gate)

    def test_a_child_with_no_time_at_all_waits_for_a_turn_after_its_task_line(self):
        self.p.records({"type": "session_meta", "payload": {"forked_from_id": "p"}})
        self.assertEqual(self.p.gate, -1.0)
        started = parse_timestamp("2026-10-03T10:00:00Z")
        self.p.records(_codex("event_msg", {"type": "task_started", "started_at": started - 1}))
        self.assertEqual(self.p.gate, -1.0)
        self.p.records({"type": "event_msg", "payload": {"type": "task_started", "started_at": started}})
        self.assertEqual(self.p.gate, -1.0)  # no line time: nothing can pass
        self.p.records(_codex("event_msg", {"type": "task_started", "started_at": started}))
        self.assertIsNone(self.p.gate)

    def test_tiers(self):
        self.assertEqual(parsers.normalized_tier("Priority"), "priority")
        self.assertEqual(parsers.normalized_tier("flex"), "flex")
        self.p.records(_codex("event_msg", {"type": "thread_settings_applied", "service_tier": "flex"}))
        self.assertEqual(self.p.tier, "flex")
        self.p.records(_codex("event_msg", {"type": "thread_settings_applied", "thread_settings": {}}))
        self.assertEqual(self.p.tier, "flex")

    def test_file_reader_skips_noise_lines(self):
        f = temp_dir(self) / "rollout-y.jsonl"
        f.write_text("\n".join([
            '{"type": "response_item"}',
            '{"type": "token_count" broken',
            '["token_count"]',
            json.dumps(_tc({"input_tokens": 3, "output_tokens": 1})),
        ]) + "\n")
        self.assertEqual([r.event.input for r in parsers.codex_file_records(f, "/c")], [3])


class TestFileLines(unittest.TestCase):
    def test_an_oversized_line_is_skipped_but_moves_the_offset(self):
        f = temp_dir(self) / "big.jsonl"
        f.write_bytes(b"a" * 40 + b"\n" + b"ok\n")
        end = []
        orig = parsers.MAX_LINE
        parsers.MAX_LINE = 10
        try:
            self.assertEqual(list(parsers.iter_lines(f, end=end)), [b"ok\n"])
        finally:
            parsers.MAX_LINE = orig
        self.assertEqual(end, [44])
        with self.assertRaises(OSError):
            list(parsers.iter_lines(f.parent / "missing.jsonl"))

    def test_a_zstd_frame_is_read_as_its_plain_lines(self):
        import shutil
        import subprocess

        if shutil.which("zstd") is None:
            self.skipTest("zstd is not installed")
        f = temp_dir(self) / "copy.jsonl"
        body = b'{"type":"user"}\n{"type":"assistant"}\n'
        f.write_bytes(subprocess.run(
            ["zstd", "-3", "-q", "-c"], input=body, capture_output=True, check=True).stdout)
        end = []
        self.assertEqual(list(parsers.iter_lines(f, end=end)),
                         [b'{"type":"user"}\n', b'{"type":"assistant"}\n'])
        self.assertEqual(end, [len(body)])


class TestDatabaseValues(unittest.TestCase):
    def test_token_and_watermark_columns_read_as_sqlite_would(self):
        self.assertEqual([parsers._db_tokens(v) for v in ("12", "1e3", "lots", None, -5, 7)],
                         [12, 1000, 0, 0, 0, 7])
        self.assertEqual([parsers._row_int(v) for v in (None, "x", "7.5", 3.9, 4)], [0, 0, 7, 3, 4])

    def _devin(self, rows, sessions=False):
        import sqlite3

        db = temp_dir(self) / "sessions.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE message_nodes (row_id INTEGER, session_id TEXT, created_at REAL, chat_message TEXT)")
        if sessions:
            conn.execute("CREATE TABLE sessions (id TEXT, working_directory TEXT)")
        conn.executemany("INSERT INTO message_nodes VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        return db

    def test_devin_without_a_sessions_table_or_a_created_stamp(self):
        def node(rid, metrics, created=None):
            meta = {"request_id": rid, "generation_model": "swe-2", "metrics": metrics}
            if created:
                meta["created_at"] = created
            return json.dumps({"role": "assistant", "metadata": meta})
        db = self._devin([
            (1, None, 1759500000.5, node("r1", {"input_tokens": 3, "output_tokens": 1})),
            (2, "d1", 0, node("r2", {"input_tokens": 0, "output_tokens": 0}, "2026-10-03T10:00:00Z")),
        ])
        recs, highest = parsers.devin_scan(db, 0)
        self.assertEqual([(r.key, r.event.session, r.event.ts, r.event.cwd) for r in recs],
                         [("devin:r1", "unknown", 1759500000.5, None)])
        self.assertEqual(highest, 2)
        # A watermark past every row means the file was rebuilt: read it again.
        self.assertEqual(len(parsers.devin_scan(db, 99)[0]), 1)


class TestPiAndDroidEdges(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir(self)

    def test_pi_reader_skips_noise_lines(self):
        f = self.dir / "s.jsonl"
        good = {"type": "message", "id": "m1", "message": {
            "role": "assistant", "timestamp": 1759500000000, "usage": {"input": 1, "output": 1}}}
        f.write_text("\n".join([
            '{"type": "other"}', '{"message" broken', '["message"]',
            json.dumps({"type": "session", "cwd": ""}),
            json.dumps({"type": "model_change", "message": "x"}),
            json.dumps(good)]) + "\n")
        recs = parsers.pi_file_records(f, "/p")
        self.assertEqual([(r.key, r.event.cwd) for r in recs], [("pi:s.jsonl:m1", None)])

    def test_droid_settings_must_be_an_object(self):
        settings = self.dir / "u.settings.json"
        settings.write_text("[1, 2]")
        with self.assertRaisesRegex(ValueError, "not a JSON object"):
            parsers.droid_session_totals(settings, "/d")

    def test_droid_transcript_noise_and_a_failed_read(self):
        settings = self.dir / "u.settings.json"
        settings.write_text(json.dumps({"tokenUsage": {"inputTokens": 5}}))
        (self.dir / "u.jsonl").write_text("\n".join([
            "[1]", json.dumps({"type": "session_start", "cwd": "/w"}),
            json.dumps({"type": "session_start", "cwd": "/later"}),
            json.dumps({"type": "message", "timestamp": "2026-10-01T10:05:00Z"}),
            json.dumps({"type": "message", "timestamp": "never"})]) + "\n")
        rec = parsers.droid_session_totals(settings, "/d")
        self.assertEqual((rec.event.cwd, rec.event.ts),
                         ("/w", parse_timestamp("2026-10-01T10:05:00Z")))

        class Failing:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                yield json.dumps({"type": "session_start", "cwd": "/w"}).encode()
                raise OSError("I/O error")

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("u.jsonl"):
                return Failing()
            return real_open(path, *args, **kwargs)

        from unittest.mock import patch

        with patch("builtins.open", side_effect=fake_open):
            rec = parsers.droid_session_totals(settings, "/d")
        self.assertEqual((rec.event.cwd, rec.event.ts), ("/w", settings.stat().st_mtime))


if __name__ == "__main__":
    unittest.main()
