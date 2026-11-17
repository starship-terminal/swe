"""ledger/parsers.py session files: Pi logs and the Droid, Cline and
Grok session files (the ``SessionFileParsers.swift`` readers), their
scan wiring, and the store's authoritative upsert."""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from swe.ledger import parsers
from swe.ledger.models import LedgerRecord, UsageEvent
from swe.ledger.scan import scan
from swe.ledger.store import LedgerStore, ledger_path_for

from tests._tempdir import temp_dir


def _pi_message(mid, ts_ms, usage, role="assistant", model="pi-model"):
    return json.dumps({"type": "message", "id": mid, "message": {
        "role": role, "model": model, "timestamp": ts_ms, "usage": usage}}) + "\n"


def _pi_usage(inp=100, out=50, **kw):
    return {"input": inp, "output": out,
            "cost": {"input": 0.001, "output": 0.002, "total": 0.003}, **kw}


class TestPi(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.sessions = self.home / ".pi" / "agent" / "sessions" / "work-proj"
        self.sessions.mkdir(parents=True)
        self.path = self.sessions / "2026-10-01T10-00-00_deadbeef.jsonl"

    def _write(self, text):
        self.path.write_text(text)
        return parsers.pi_file_records(self.path, str(self.home / ".pi"))

    def test_message_line_and_session_cwd(self):
        recs = self._write(
            json.dumps({"type": "session", "cwd": "/work/proj"}) + "\n"
            + _pi_message("m1", 1759500000000, _pi_usage(
                reasoning=20, cacheRead=30, cacheWrite=10)))
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.key, "pi:2026-10-01T10-00-00_deadbeef.jsonl:m1")
        ev = rec.event
        self.assertEqual((ev.harness, ev.account, ev.session),
                         ("pi", str(self.home / ".pi"), str(self.path)))
        self.assertEqual(ev.cwd, "/work/proj")
        self.assertEqual(ev.model, "pi-model")
        self.assertEqual(ev.ts, 1759500000000 / 1000)
        # usage.output includes reasoning: 50 - 20 back out.
        self.assertEqual((ev.input, ev.output, ev.reasoning,
                          ev.cache_read, ev.cache_write), (100, 30, 20, 30, 10))
        self.assertEqual(ev.cost, 0.003)

    def test_lines_that_do_not_count(self):
        recs = self._write(
            _pi_message("u1", 1759500000000, _pi_usage(), role="user")
            + _pi_message("m2", 1759500000000, None)
            + json.dumps({"type": "message", "message": {
                "role": "assistant", "usage": _pi_usage()}}) + "\n"  # no timestamp
            + _pi_message("m3", 1759500000000, {"input": 0, "output": 0})
            + '{"type": "session"')  # partial line waits for the next read
        self.assertEqual(recs, [])

    def test_reasoning_over_output_is_clamped(self):
        # usage.output includes reasoning, so reasoning can never exceed
        # it; a log that says otherwise gives all of output to reasoning
        # instead of a negative output.
        recs = self._write(
            _pi_message("m1", 1759500000000, _pi_usage(out=50, reasoning=80)))
        ev = recs[0].event
        self.assertEqual((ev.output, ev.reasoning), (0, 50))

    def test_no_id_keys_on_timestamp_and_zero_tokens_with_cost_count(self):
        # A zero-token, cost-less request is dropped; a cost-only one counts.
        recs = self._write(
            _pi_message(None, 1759500000000, _pi_usage(inp=0, out=0, cost=None))
            + _pi_message(None, 1759500000001,
                          {"input": 0, "output": 0, "cost": {"total": 0.01}}))
        self.assertEqual([r.key for r in recs],
                         ["pi:2026-10-01T10-00-00_deadbeef.jsonl:1759500000001.000"])

    def test_a_later_session_line_supplies_its_cwd(self):
        recs = self._write(
            json.dumps({"type": "session", "cwd": "/a"}) + "\n"
            + _pi_message("m1", 1759500000000, _pi_usage())
            + json.dumps({"type": "session", "cwd": "/b"}) + "\n"
            + _pi_message("m2", 1759500000001, _pi_usage()))
        self.assertEqual([r.event.cwd for r in recs], ["/a", "/b"])


def _droid_settings(usage=None, model="gpt-5.1-codex", **kw):
    return json.dumps({"model": model,
                       "inclusiveTokenUsage": usage if usage is not None else {
                           "inputTokens": 1000, "outputTokens": 50,
                           "cacheReadTokens": 200, "cacheCreationTokens": 30,
                           "thinkingTokens": 5}, **kw})


class TestDroid(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.folder = self.home / ".factory" / "sessions" / "proj"
        self.folder.mkdir(parents=True)
        self.settings = self.folder / "u-u-i-d.settings.json"
        self.account = str(self.home / ".factory")

    def _transcript(self, *lines):
        (self.folder / "u-u-i-d.jsonl").write_text("".join(lines))

    def test_totals_transcript_cwd_and_last_timestamp(self):
        self.settings.write_text(_droid_settings())
        self._transcript(
            json.dumps({"type": "session_start", "cwd": "/work",
                        "timestamp": "2026-10-01T10:00:00Z"}) + "\n",
            json.dumps({"type": "message",
                        "timestamp": "2026-10-01T10:05:00Z"}) + "\n",
            "not json\n",
        )
        rec = parsers.droid_session_totals(self.settings, self.account)
        self.assertIsInstance(rec, LedgerRecord)
        self.assertEqual(rec.key, "droid:u-u-i-d")
        self.assertTrue(rec.authoritative)
        ev = rec.event
        self.assertEqual((ev.harness, ev.session, ev.cwd, ev.model),
                         ("droid", "u-u-i-d", "/work", "gpt-5.1-codex"))
        self.assertEqual((ev.input, ev.output, ev.cache_read, ev.cache_write,
                          ev.reasoning), (1000, 50, 200, 30, 5))
        self.assertTrue(ev.session_total)
        self.assertEqual(ev.ts, datetime(2026, 10, 1, 10, 5,
                                         tzinfo=timezone.utc).timestamp())

    def test_tokenusage_fallback_and_mtimestamps(self):
        usage = {"inputTokens": 10, "outputTokens": 1}
        self.settings.write_text(json.dumps({"tokenUsage": usage}))
        rec = parsers.droid_session_totals(self.settings, self.account)
        self.assertIsInstance(rec, LedgerRecord)
        self.assertEqual((rec.event.input, rec.event.output), (10, 1))
        # No transcript: the settings file's own mtime is the timestamp.
        self.assertEqual(rec.event.ts, self.settings.stat().st_mtime)

    def test_zero_or_missing_usage_is_empty(self):
        self.settings.write_text(_droid_settings(usage={}))
        self.assertEqual(parsers.droid_session_totals(
            self.settings, self.account).key, "droid:u-u-i-d")
        self.settings.write_text(json.dumps({"model": "m"}))
        empty = parsers.droid_session_totals(self.settings, self.account)
        self.assertIsInstance(empty, parsers.Empty)


def _cline_manifest(tokens=True, cost=None, **kw):
    metadata = {}
    if tokens:
        metadata = {"tokensIn": 100, "tokensOut": 50,
                    "cacheReads": 30, "cacheWrites": 10}
    if cost is not None:
        metadata["totalCost"] = cost
    return {"session_id": "s1", "model": "cline-model", "cwd": "/work",
            "started_at": "2026-10-01T10:00:00Z",
            "ended_at": "2026-10-01T10:30:00Z", "metadata": metadata, **kw}


class TestCline(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.folder = self.home / ".cline" / "data" / "sessions" / "s1"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "s1.json"
        self.account = str(self.home / ".cline")

    def test_manifest_totals(self):
        self.path.write_text(json.dumps(_cline_manifest(cost=0.02)))
        rec = parsers.cline_session_totals(self.path, self.account)
        self.assertIsInstance(rec, LedgerRecord)
        self.assertEqual((rec.key, rec.authoritative), ("cline:s1", True))
        ev = rec.event
        self.assertEqual((ev.harness, ev.session, ev.cwd, ev.model),
                         ("cline", "s1", "/work", "cline-model"))
        self.assertEqual((ev.input, ev.output, ev.cache_read, ev.cache_write),
                         (100, 50, 30, 10))
        self.assertTrue(ev.session_total)
        self.assertEqual(ev.cost, 0.02)
        # ended_at wins over started_at.
        self.assertEqual(ev.ts, datetime(2026, 10, 1, 10, 30,
                                         tzinfo=timezone.utc).timestamp())

    def test_cost_only_counts_zero_cost_is_none_started_at_fallback(self):
        self.path.write_text(json.dumps(_cline_manifest(
            tokens=False, cost=0.01, ended_at=None)))
        rec = parsers.cline_session_totals(self.path, self.account)
        self.assertIsInstance(rec, LedgerRecord)
        # started_at is the fallback timestamp.
        self.assertEqual(rec.event.ts, datetime(2026, 10, 1, 10, 0,
                                                tzinfo=timezone.utc).timestamp())
        self.assertEqual(rec.event.cost, 0.01)
        self.path.write_text(json.dumps(_cline_manifest(cost=0)))
        rec = parsers.cline_session_totals(self.path, self.account)
        self.assertIsNone(rec.event.cost)

    def test_workspace_root_and_missing_session_id(self):
        manifest = _cline_manifest()
        # ``cwd ?? workspace_root``: only an absent cwd falls back; a
        # present null suppresses the root like Swift's ``??`` does.
        del manifest["cwd"]
        manifest["workspace_root"] = "/root"
        self.path.write_text(json.dumps(manifest))
        rec = parsers.cline_session_totals(self.path, self.account)
        self.assertEqual(rec.event.cwd, "/root")
        self.path.write_text(json.dumps({"metadata": {"tokensIn": 1}}))
        self.assertIsNone(parsers.cline_session_totals(self.path, self.account))

    def test_zero_totals_and_no_timestamp_are_empty(self):
        self.path.write_text(json.dumps(_cline_manifest(tokens=False)))
        self.assertIsInstance(
            parsers.cline_session_totals(self.path, self.account), parsers.Empty)
        self.path.write_text(json.dumps(_cline_manifest(
            ended_at=None, started_at=None)))
        self.assertIsInstance(
            parsers.cline_session_totals(self.path, self.account), parsers.Empty)


def _grok_turn(number, ended="2026-10-01T10:00:00Z", ticks=20_000_000_000, **kw):
    turn = {
        "turnNumber": number, "endedAt": ended, "inputTokens": 1000,
        "outputTokens": 40, "cachedReadTokens": 800, "cacheCreationTokens": 0,
        "reasoningTokens": 12, "costUsdTicks": ticks, "primaryModelId": "grok-4.7",
    }
    turn.update(kw)
    return turn


def _grok_usage(turns, session=None):
    body = {"sessionId": "s1", "turns": turns}
    if session is not None:
        body["session"] = session
    return json.dumps(body)


class TestGrok(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.folder = self.home / ".grok" / "sessions" / "proj" / "s1"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "usage.json"
        self.account = str(self.home / ".grok")

    def _records(self):
        rows = parsers.grok_session_turns(self.path, self.account)
        return [r for r in rows if isinstance(r, LedgerRecord)]

    def test_one_event_per_turn_ignores_the_session_total(self):
        self.path.write_text(_grok_usage(
            [_grok_turn(1), _grok_turn(2, ended="2026-10-01T11:00:00Z", ticks=5_000_000_000)],
            session={"inputTokens": 999999, "costUsdTicks": 9_000_000_000_000}))
        (self.folder / "prompt_context.json").write_text(
            json.dumps({"working_directory": "/work/app"}))
        recs = self._records()
        self.assertEqual([r.key for r in recs], ["grok:s1:1", "grok:s1:2"])
        ev = recs[0].event
        self.assertEqual((ev.harness, ev.session, ev.cwd, ev.model),
                         ("grok", "s1", "/work/app", "grok-4.7"))
        self.assertEqual((ev.input, ev.output, ev.cache_read, ev.cache_write, ev.reasoning),
                         (1000, 40, 800, 0, 12))
        self.assertFalse(ev.session_total)
        self.assertTrue(recs[0].authoritative)
        # 20_000_000_000 ticks is $2.
        self.assertEqual(ev.cost, 2.0)
        self.assertEqual(ev.ts, datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(recs[1].event.cost, 0.5)

    def test_zero_cost_falls_through_and_a_blank_turn_empties(self):
        self.path.write_text(_grok_usage([
            _grok_turn(1, ticks=0),
            _grok_turn(2, inputTokens=0, outputTokens=0, cachedReadTokens=0,
                       reasoningTokens=0, ticks=0),
        ]))
        rows = parsers.grok_session_turns(self.path, self.account)
        self.assertIsNone(rows[0].event.cost)
        self.assertIsInstance(rows[1], parsers.Empty)
        self.assertEqual(rows[1].key, "grok:s1:2")

    def test_a_missing_timestamp_and_a_duplicate_turn_are_skipped(self):
        self.path.write_text(_grok_usage([
            _grok_turn(1, endedAt=None),
            _grok_turn(3),
            _grok_turn(3, ticks=9_000_000_000),
            {"turnNumber": True, "inputTokens": 5},
        ]))
        rows = parsers.grok_session_turns(self.path, self.account)
        self.assertIsInstance(rows[0], parsers.Empty)
        recs = [r for r in rows if isinstance(r, LedgerRecord)]
        self.assertEqual([r.key for r in recs], ["grok:s1:3"])
        self.assertEqual(recs[0].event.cost, 2.0)

    def test_no_turns_reports_nothing_kept(self):
        self.path.write_text(_grok_usage([]))
        rows = parsers.grok_session_turns(self.path, self.account)
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0], parsers.GrokDropped)
        self.assertEqual(rows[0].kept, set())


class TestSessionScan(unittest.TestCase):
    """The session sources through ``scan()``: discovery, the
    authoritative re-read, watched-sibling stamps and deletion."""

    def setUp(self):
        self.home = temp_dir(self)
        self.store = LedgerStore(ledger_path_for(self.home))
        self.addCleanup(self.store.close)

    def _rows(self):
        return {r[0]: r[1:] for r in self.store.db.execute(
            "SELECT key, harness, session, input, output, cache_read, cache_write,"
            " reasoning, session_total, ts, cwd FROM events")}

    def test_pi_log(self):
        folder = self.home / ".pi" / "agent" / "sessions" / "proj"
        folder.mkdir(parents=True)
        (folder / "t_uuid.jsonl").write_text(
            json.dumps({"type": "session", "cwd": "/w"}) + "\n"
            + _pi_message("m1", 1759500000000, _pi_usage()))
        scan(self.store, self.home)
        rows = self._rows()
        self.assertIn("pi:t_uuid.jsonl:m1", rows)
        self.assertEqual(rows["pi:t_uuid.jsonl:m1"][0:2], ("pi", str(
            folder / "t_uuid.jsonl")))
        self.assertEqual(scan(self.store, self.home).files_read, 0)

    def test_droid_replaces_and_empties(self):
        folder = self.home / ".factory" / "sessions" / "proj"
        folder.mkdir(parents=True)
        settings = folder / "u1.settings.json"
        settings.write_text(_droid_settings())
        scan(self.store, self.home)
        self.assertEqual(self._rows()["droid:u1"][2:4], (1000, 50))

        # Totals went down: the file is authoritative, larger-wins does
        # not apply.
        usage = {"inputTokens": 4, "outputTokens": 2}
        settings.write_text(_droid_settings(usage=usage))
        scan(self.store, self.home)
        self.assertEqual(self._rows()["droid:u1"][2:4], (4, 2))
        self.assertEqual(self._rows()["droid:u1"][7], 1)  # session_total

        # A file that now reports no usage deletes its event.
        settings.write_text(_droid_settings(usage={}))
        scan(self.store, self.home)
        self.assertNotIn("droid:u1", self._rows())

    def test_droid_transcript_stamp_re_reads(self):
        folder = self.home / ".factory" / "sessions" / "proj"
        folder.mkdir(parents=True)
        settings = folder / "u2.settings.json"
        settings.write_text(_droid_settings())
        transcript = folder / "u2.jsonl"
        transcript.write_text(json.dumps(
            {"type": "session_start", "cwd": "/a",
             "timestamp": "2026-10-01T10:00:00Z"}) + "\n")
        scan(self.store, self.home)
        self.assertEqual(self._rows()["droid:u2"][9], "/a")
        first_ts = self._rows()["droid:u2"][9 - 1]

        # Only the transcript moved — settings stat is untouched — but its
        # stamp is part of the cursor, so the pair is re-read.
        transcript.write_text(
            json.dumps({"type": "session_start", "cwd": "/b",
                        "timestamp": "2026-10-01T10:00:00Z"}) + "\n"
            + json.dumps({"type": "message",
                          "timestamp": "2026-10-02T10:00:00Z"}) + "\n")
        result = scan(self.store, self.home)
        self.assertEqual(result.files_read, 1)
        row = self._rows()["droid:u2"]
        self.assertEqual(row[9], "/b")
        self.assertGreater(row[8], first_ts)

    def test_cline_discovery_replacement_and_non_session_files(self):
        session = self.home / ".cline" / "data" / "sessions" / "s1"
        session.mkdir(parents=True)
        manifest = session / "s1.json"
        manifest.write_text(json.dumps(_cline_manifest(cost=0.5)))
        # The transcript sibling is never the totals file (stem differs).
        (session / "s1.messages.json").write_text("{}")
        other = session.parent / "notasession"
        other.mkdir()
        (other / "s1.json").write_text("{}")
        scan(self.store, self.home)
        rows = self._rows()
        self.assertEqual(set(rows), {"cline:s1"})
        self.assertEqual(rows["cline:s1"][7], 1)

        manifest.write_text(json.dumps(_cline_manifest(
            tokens=True, ended_at="2026-10-03T10:30:00Z",
            metadata={"tokensIn": 7, "tokensOut": 3, "cacheReads": 0,
                      "cacheWrites": 0})))
        scan(self.store, self.home)
        self.assertEqual(self._rows()["cline:s1"][2:4], (7, 3))

        manifest.write_text(json.dumps(_cline_manifest(tokens=False, cost=None)))
        scan(self.store, self.home)
        self.assertNotIn("cline:s1", self._rows())

    def test_corrupt_session_file_is_retried(self):
        session = self.home / ".cline" / "data" / "sessions" / "s1"
        session.mkdir(parents=True)
        manifest = session / "s1.json"
        manifest.write_text("{bad json")
        result = scan(self.store, self.home)
        self.assertEqual(len(result.errors), 1)
        manifest.write_text(json.dumps(_cline_manifest()))
        scan(self.store, self.home)
        self.assertIn("cline:s1", self._rows())

    def test_grok_discovery_replacement_and_a_dropped_turn(self):
        folder = self.home / ".grok" / "sessions" / "proj" / "s1"
        folder.mkdir(parents=True)
        usage = folder / "usage.json"
        usage.write_text(_grok_usage([_grok_turn(1), _grok_turn(2, ticks=4_000_000_000)]))
        # A namesake one level too deep is not a session file.
        nested = folder / "extra"
        nested.mkdir()
        (nested / "usage.json").write_text(_grok_usage([_grok_turn(9)]))
        scan(self.store, self.home)
        rows = self._rows()
        self.assertEqual(set(rows), {"grok:s1:1", "grok:s1:2"})
        self.assertEqual(rows["grok:s1:1"][7], 0)  # a turn, not a session total

        usage.write_text(_grok_usage([
            _grok_turn(1, inputTokens=7, outputTokens=3, ticks=1_000_000_000)]))
        scan(self.store, self.home)
        rows = self._rows()
        self.assertEqual(set(rows), {"grok:s1:1"})
        self.assertEqual(rows["grok:s1:1"][2:4], (7, 3))
        # A session whose id starts with this one keeps its own rows.
        other = self.home / ".grok" / "sessions" / "proj" / "s1-extra"
        other.mkdir()
        (other / "usage.json").write_text(_grok_usage([_grok_turn(1)]))
        scan(self.store, self.home)
        self.assertIn("grok:s1-extra:1", self._rows())
        self.assertIn("grok:s1:1", self._rows())


class TestSessionTotalsInStore(unittest.TestCase):
    """``session_total`` rows never count toward long-context surcharges."""

    def setUp(self):
        self.store = LedgerStore(temp_dir(self) / "l.sqlite")
        self.addCleanup(self.store.close)

    def test_groups_marks_session_totals_not_long(self):
        for flag in (True, False):
            self.store.upsert(LedgerRecord(key=f"k{flag}", event=UsageEvent(
                harness="droid", account="/h", session="s", cwd=None,
                model="m", ts=1000.0, input=1000, session_total=flag)))
        rows = self.store.groups(None, None, {"m": 100})
        # Same bucket and dims, so the rows differ only in the long flag:
        # the session total never crosses the threshold, the request does.
        self.assertEqual(sorted(r[4] for r in rows), [0, 1])


class TestAuthoritativeUpsert(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(temp_dir(self) / "l.sqlite")
        self.addCleanup(self.store.close)

    def _ev(self, tokens, ts=1000.0):
        return UsageEvent(harness="droid", account="/h", session="s", cwd=None,
                          model="m", ts=ts, input=tokens)

    def test_replaces_even_when_smaller_and_identical_is_a_noop(self):
        self.store.upsert(LedgerRecord(key="k", event=self._ev(100),
                                     authoritative=True))
        self.store.upsert(LedgerRecord(key="k", event=self._ev(40),
                                     authoritative=True))
        row = self.store.db.execute("SELECT input FROM events WHERE key='k'").fetchone()
        self.assertEqual(row[0], 40)
        self.assertFalse(self.store.upsert(
            LedgerRecord(key="k", event=self._ev(40), authoritative=True)))
        # A non-authoritative re-read still follows the larger-wins rule.
        self.assertFalse(self.store.upsert(LedgerRecord(key="k", event=self._ev(4))))

    def test_delete_removes_the_row(self):
        self.store.upsert(LedgerRecord(key="k", event=self._ev(100)))
        self.store.delete("k")
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM events WHERE key='k'").fetchone())

    def test_lookalike_row_is_removed(self):
        # An older key scheme left a row for the same event under another
        # key; the authoritative insert removes it, or it double-counts.
        self.store.upsert(LedgerRecord(key="old-key", event=self._ev(100)))
        self.store.upsert(LedgerRecord(key="new-key", event=self._ev(100),
                                     authoritative=True))
        keys = [r[0] for r in self.store.db.execute("SELECT key FROM events")]
        self.assertEqual(keys, ["new-key"])


if __name__ == "__main__":
    unittest.main()
