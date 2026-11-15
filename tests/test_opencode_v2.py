"""OpenCode 2.x's ``session_v2`` / ``session_message`` store next to 1.x's.

OpenCode 2.x writes new sessions only to ``session_v2`` and every turn
only to ``session_message``. Its migration copied old sessions into
``session_v2`` and most of their turns into ``session_message`` under
the same ids. The fixture holds both shapes the way a real migrated
``opencode.db`` does: a legacy session in both tables (one turn copied),
and 2.x sessions only in the v2 tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.sessions.models import Session

T0 = 1_792_604_440_000  # epoch ms

_SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
  directory TEXT NOT NULL, title TEXT NOT NULL, workspace_id TEXT,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE workspace (id TEXT PRIMARY KEY, provider TEXT NOT NULL);
CREATE TABLE message (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (
  id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE session_v2 (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT, parent_id TEXT,
  directory TEXT NOT NULL, title TEXT, version TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, time_archived INTEGER);
CREATE TABLE session_message (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


def _assistant(content, finish="stop", completed=True, model="MiniMax-M3", tokens=None):
    data = {
        "time": {"created": T0, **({"completed": T0 + 1} if completed else {})},
        "model": {"id": model, "providerID": "minimax"},
        "content": content,
        "finish": finish,
        "cost": 0.002,
        "tokens": tokens or {"input": 100, "output": 10, "reasoning": 4, "cache": {"read": 50, "write": 0}},
    }
    if finish is None:
        del data["finish"]
    return data


def _shell(command, output, status="completed"):
    return {
        "type": "tool", "id": f"call_{command}", "name": "shell",
        "state": {"status": status, "input": {"command": command},
                  "content": [{"type": "text", "text": output}]},
    }


class OpencodeV2Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        patcher = patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.db = self.home / ".local/share/opencode/opencode.db"
        self.db.parent.mkdir(parents=True)
        conn = self.conn()
        conn.executescript(_SCHEMA)
        # 1.x session, copied into session_v2 by the migration (title left
        # NULL there) with its first turn copied under the same id.
        conn.execute("INSERT INTO session VALUES ('ses_old', 'p', NULL, '/work/old', 'old work', NULL, ?, ?)",
                     (T0 - 9000, T0 - 5000))
        conn.execute("INSERT INTO session_v2 VALUES ('ses_old', 'p', NULL, NULL, '/work/old', NULL, '1.18.30', ?, ?, NULL)",
                     (T0 - 9000, T0 - 5000))
        conn.execute("INSERT INTO message VALUES ('msg_u', 'ses_old', ?, ?, ?)",
                     (T0 - 9000, T0 - 9000, json.dumps({"role": "user"})))
        conn.execute("INSERT INTO part VALUES ('prt_u', 'msg_u', 'ses_old', ?, ?, ?)",
                     (T0 - 9000, T0 - 9000, json.dumps({"type": "text", "text": "fix the old bug"})))
        conn.execute("INSERT INTO message VALUES ('msg_a', 'ses_old', ?, ?, ?)",
                     (T0 - 8000, T0 - 8000, json.dumps({
                         "role": "assistant", "finish": "stop", "modelID": "grok-4.7", "providerID": "xai",
                         "tokens": {"total": 30, "input": 20, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}}})))
        conn.execute("INSERT INTO part VALUES ('prt_a', 'msg_a', 'ses_old', ?, ?, ?)",
                     (T0 - 8000, T0 - 8000, json.dumps({"type": "text", "text": "Fixed the old bug."})))
        self.v2_row(conn, "msg_u", "ses_old", "user", 0, {"text": "fix the old bug"}, T0 - 9000)
        # 2.x-only sessions.
        self.v2_session(conn, "ses_new", "/work/new", "Greeting", T0, T0 + 4000)
        self.v2_row(conn, "m1", "ses_new", "user", 4, {"text": "hello there", "files": []}, T0)
        self.v2_row(conn, "m2", "ses_new", "system", 5, {"text": "Instructions updated"}, T0 + 10)
        self.v2_row(conn, "m3", "ses_new", "assistant", 7, _assistant(
            [{"type": "reasoning", "text": "the user greets me"},
             _shell("ls", "README.md"),
             {"type": "text", "text": "Hey. What are we working on?"}]), T0 + 1000)
        self.v2_row(conn, "m4", "ses_new", "idle", 12, {"outcome": "succeeded"}, T0 + 4000)
        conn.commit()
        conn.close()

    def conn(self):
        return sqlite3.connect(self.db)

    @staticmethod
    def v2_session(conn, sid, directory, title, created, updated):
        conn.execute("INSERT INTO session_v2 VALUES (?, 'p', NULL, NULL, ?, ?, '2.1.3', ?, ?, NULL)",
                     (sid, directory, title, created, updated))

    @staticmethod
    def v2_row(conn, mid, sid, kind, seq, data, created):
        conn.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (mid, sid, kind, seq, created, created, json.dumps(data)))

    def add_session(self, sid, rows):
        conn = self.conn()
        self.v2_session(conn, sid, "/work/" + sid, sid, T0, T0)
        for seq, (kind, data) in enumerate(rows):
            self.v2_row(conn, f"{sid}_{seq}", sid, kind, seq, data, T0 + seq)
        conn.commit()
        conn.close()

    @staticmethod
    def session(sid, age_s=9999):
        import time
        return Session(timestamp=(time.time() - age_s) * 1000, agent="OpenCode", path="/work",
                       title="t", session_id=sid, tool_name="opencode")


class ParseTest(OpencodeV2Case):
    def test_lists_each_session_once_from_both_tables(self):
        from swe.sessions.parsers import parse_opencode

        sessions = {s.session_id: s for s in parse_opencode()}
        self.assertEqual(set(sessions), {"ses_old", "ses_new"})
        self.assertEqual(sessions["ses_new"].path, "/work/new")
        self.assertEqual(sessions["ses_new"].title, "Greeting")
        self.assertEqual(sessions["ses_new"].timestamp, T0 + 4000)
        # The v2 copy has no title: the legacy row's fills in.
        self.assertEqual(sessions["ses_old"].title, "old work")
        self.assertEqual(sessions["ses_old"].path, "/work/old")

    def test_a_session_resumed_in_2x_takes_the_newer_time(self):
        from swe.sessions.parsers import parse_opencode

        conn = self.conn()
        conn.execute("UPDATE session_v2 SET time_updated = ?, title = 'renamed' WHERE id = 'ses_old'", (T0 + 9000,))
        conn.commit()
        conn.close()
        old = next(s for s in parse_opencode() if s.session_id == "ses_old")
        self.assertEqual(old.timestamp, T0 + 9000)
        self.assertEqual(old.title, "renamed")

    def test_a_legacy_only_database_still_reads(self):
        from swe.sessions.parsers import parse_opencode

        conn = self.conn()
        conn.executescript("DROP TABLE session_v2; DROP TABLE session_message;")
        conn.commit()
        conn.close()
        self.assertEqual([s.session_id for s in parse_opencode()], ["ses_old"])


class StatusTest(OpencodeV2Case):
    def probe(self, sid):
        from swe.sessions import status

        conn = status._open_opencode_db()
        try:
            return status._probe_opencode(self.session(sid), {"opencode_conn": conn})
        finally:
            conn.close()

    def test_idle_after_a_reply_is_finished_with_its_text(self):
        self.assertEqual(self.probe("ses_new"), ("finished", "Hey. What are we working on?"))

    def test_legacy_session_still_reads_its_message_rows(self):
        self.assertEqual(self.probe("ses_old"), ("finished", "Fixed the old bug."))

    def test_prompt_and_tool_calls_are_midturn(self):
        self.add_session("s_prompt", [("user", {"text": "go"}), ("system", {"text": "x"})])
        self.add_session("s_tools", [("user", {"text": "go"}),
                                     ("assistant", _assistant([_shell("ls", "a")], finish="tool-calls"))])
        self.add_session("s_synth", [("assistant", _assistant([{"type": "text", "text": "hi"}])),
                                     ("synthetic", {"text": "Continue if you have next steps"})])
        for sid in ("s_prompt", "s_tools", "s_synth"):
            self.assertEqual(self.probe(sid), ("midturn", ""), sid)

    def test_a_pass_reads_the_shape_once_and_only_the_deciding_row(self):
        # The status column probes every session each pass: the table
        # shape is read once per connection, and only the newest decisive
        # row's JSON is fetched, not the whole 50-row window.
        from swe.sessions import status

        sids = ["s_a", "s_b", "s_c"]
        for sid in sids:
            self.add_session(sid, [("user", {"text": "go"})] + [
                ("assistant", _assistant([{"type": "text", "text": f"reply {i}"}]))
                for i in range(10)
            ])
        conn = status._open_opencode_db()
        statements = []
        conn.set_trace_callback(statements.append)
        ctx = {"opencode_conn": conn}
        try:
            for sid in sids:
                self.assertEqual(status._probe_opencode(self.session(sid), ctx),
                                 ("finished", "reply 9"))
        finally:
            conn.close()
        self.assertEqual(sum("PRAGMA" in s for s in statements), 2)
        self.assertFalse([s for s in statements if "sm.data" in s])
        self.assertEqual(sum(s.startswith("SELECT data FROM session_message") for s in statements),
                         len(sids))

    def test_idle_skips_a_row_whose_json_does_not_decode(self):
        self.add_session("s_bad", [("assistant", _assistant([{"type": "text", "text": "Earlier."}])),
                                   ("assistant", {"x": 1}), ("idle", {"outcome": "succeeded"})])
        conn = self.conn()
        conn.execute("UPDATE session_message SET data = 'not json' WHERE id = 's_bad_1'")
        conn.commit()
        conn.close()
        self.assertEqual(self.probe("s_bad"), ("finished", "Earlier."))

    def test_bookkeeping_alone_keeps_the_legacy_verdict(self):
        conn = self.conn()
        self.v2_row(conn, "m_switch", "ses_old", "model-switched", 9, {"model": {"id": "x"}}, T0 + 9000)
        conn.commit()
        conn.close()
        self.assertEqual(self.probe("ses_old"), ("finished", "Fixed the old bug."))

    def test_idle_outcomes_and_finish_error(self):
        self.add_session("s_failed", [("user", {"text": "go"}), ("idle", {"outcome": "failed"})])
        self.add_session("s_stopped", [("user", {"text": "go"}), ("idle", {"outcome": "interrupted"})])
        self.add_session("s_error", [("assistant", _assistant([], finish="error"))])
        self.assertEqual(self.probe("s_failed"), ("error", ""))
        self.assertEqual(self.probe("s_stopped"), ("aborted", ""))
        self.assertEqual(self.probe("s_error"), ("error", ""))

    def test_status_column_labels(self):
        from swe.sessions.status import DONE, FOLLOWUP, INTERRUPTED, session_statuses

        self.add_session("s_done", [("assistant", _assistant([{"type": "text", "text": "Merged. CI green."}])),
                                    ("idle", {"outcome": "succeeded"})])
        self.add_session("s_tools", [("assistant", _assistant([], finish=None))])
        labels = session_statuses([self.session("s_done"), self.session("ses_new"), self.session("s_tools")])
        # ses_new's reply ends by asking the user something.
        self.assertEqual(labels, [DONE, FOLLOWUP, INTERRUPTED])


class TranscriptTest(OpencodeV2Case):
    def test_v2_session_reads_prompts_replies_and_tools(self):
        from swe.reports.transcripts import read_transcript

        transcript = read_transcript(self.session("ses_new"))
        self.assertTrue(transcript.readable)
        self.assertEqual(
            [(m.role, m.text) for m in transcript.messages],
            [("human", "hello there"), ("tool", "shell: README.md"),
             ("assistant", "Hey. What are we working on?")],
        )

    def test_copied_turns_are_not_read_twice(self):
        from swe.reports.transcripts import read_transcript

        texts = [m.text for m in read_transcript(self.session("ses_old")).messages]
        self.assertEqual(texts, ["fix the old bug", "Fixed the old bug."])

    def test_a_turn_added_in_2x_follows_the_legacy_history(self):
        from swe.reports.transcripts import read_transcript

        conn = self.conn()
        self.v2_row(conn, "m_resume", "ses_old", "user", 9, {"text": "and the next one"}, T0 + 9000)
        conn.commit()
        conn.close()
        texts = [m.text for m in read_transcript(self.session("ses_old")).messages]
        self.assertEqual(texts[-1], "and the next one")


class ModelCountTest(OpencodeV2Case):
    def test_v2_replies_count_once(self):
        from swe.sessions.models_analytics import _drizzle_model_counts

        conn = self.conn()
        # A reply the migration copied: same id as its message row.
        self.v2_row(conn, "msg_a", "ses_old", "assistant", 1, _assistant([], model="grok-4.7"), T0 - 8000)
        conn.commit()
        conn.close()
        self.assertEqual(_drizzle_model_counts(str(self.db)), {("minimax", "MiniMax-M3"): 1})


class LedgerTest(OpencodeV2Case):
    def test_v2_replies_become_usage_events_once(self):
        from swe.ledger.parsers import opencode_records

        conn = self.conn()
        self.v2_row(conn, "msg_a", "ses_old", "assistant", 1, _assistant([], model="grok-4.7"), T0 - 8000)
        conn.commit()
        conn.close()
        records = {r.key: r.event for r in opencode_records(self.db)}
        self.assertEqual(set(records), {"opencode:msg_a", "opencode:m3"})
        self.assertEqual(records["opencode:msg_a"].model, "xai/grok-4.7")
        self.assertEqual(records["opencode:msg_a"].total, 30)
        new = records["opencode:m3"]
        self.assertEqual((new.session, new.cwd, new.model), ("ses_new", "/work/new", "minimax/MiniMax-M3"))
        self.assertEqual((new.input, new.output, new.reasoning, new.cache_read), (100, 10, 4, 50))
        self.assertEqual(new.cost, 0.002)


    def test_models_are_provider_slash_model(self):
        from swe.ledger.parsers import opencode_model, opencode_records

        conn = self.conn()
        # 2.x: no provider recorded, an empty one, and an id with a slash of its own.
        self.v2_row(conn, "n1", "ses_new", "assistant", 20,
                    {**_assistant([]), "model": {"id": "bare-model"}}, T0 + 2000)
        self.v2_row(conn, "n2", "ses_new", "assistant", 21,
                    {**_assistant([]), "model": {"id": "empty-provider", "providerID": ""}}, T0 + 2100)
        self.v2_row(conn, "n3", "ses_new", "assistant", 22,
                    {**_assistant([]), "model": {"id": "nvidia/nemotron:free", "providerID": "openrouter"}},
                    T0 + 2200)
        # 1.x: the provider sits beside modelID.
        conn.execute("INSERT INTO message VALUES ('l1', 'ses_old', ?, ?, ?)", (T0 - 7000, T0 - 7000, json.dumps({
            "role": "assistant", "modelID": "gpt-5.4", "providerID": "openai",
            "tokens": {"total": 5, "input": 5, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}})))
        conn.execute("INSERT INTO message VALUES ('l2', 'ses_old', ?, ?, ?)", (T0 - 6000, T0 - 6000, json.dumps({
            "role": "assistant", "modelID": "no-provider",
            "tokens": {"total": 5, "input": 5, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}})))
        conn.commit()
        conn.close()
        models = {r.key: r.event.model for r in opencode_records(self.db)}
        self.assertEqual(models["opencode:n1"], "bare-model")
        self.assertEqual(models["opencode:n2"], "empty-provider")
        self.assertEqual(models["opencode:n3"], "openrouter/nvidia/nemotron:free")
        self.assertEqual(models["opencode:l1"], "openai/gpt-5.4")
        self.assertEqual(models["opencode:l2"], "no-provider")
        self.assertEqual(opencode_model("openrouter", "openrouter/free"), "openrouter/openrouter/free")
        self.assertEqual(opencode_model("openai", None), "unknown")
        self.assertEqual(opencode_model(None, ""), "unknown")

    def test_a_cursor_saved_before_provider_names_renames_the_rows_in_place(self):
        """A ledger that holds bare model names under the ``#v2`` cursor
        reads the database again, and each event moves to its
        ``provider/model`` name: same keys, same tokens, same cost."""
        import dataclasses

        from swe.ledger.parsers import opencode_records
        from swe.ledger.scan import scan
        from swe.ledger.models import LedgerRecord
        from swe.ledger.store import LedgerStore

        store = LedgerStore(self.home / "ledger.sqlite")
        self.addCleanup(store.db.close)
        bare = {"opencode:msg_a": "grok-4.7", "opencode:m3": "MiniMax-M3"}
        for rec in opencode_records(self.db):
            store.upsert(LedgerRecord(rec.key, dataclasses.replace(rec.event, model=bare[rec.key])))
        # Aged a minute, so the stamp this scan saves is trusted to skip
        # the next one (#1930).
        for f in (self.db, Path(str(self.db) + "-wal")):
            if f.exists():
                old = f.stat().st_mtime_ns - 60_000_000_000
                os.utime(f, ns=(old, old))
        st = self.db.stat()
        store.set_file_cursor(str(self.db) + "#v2", st.st_ino, st.st_size, st.st_mtime_ns, st.st_size)
        store.db.commit()
        before = store.db.execute(
            "SELECT COUNT(*), SUM(input + output + cache_read + cache_write + reasoning),"
            " TOTAL(cost) FROM events").fetchone()

        result = scan(store, self.home)
        self.assertEqual(result.records, 2)
        after = store.db.execute(
            "SELECT COUNT(*), SUM(input + output + cache_read + cache_write + reasoning),"
            " TOTAL(cost) FROM events").fetchone()
        self.assertEqual(after, before)
        self.assertEqual(
            dict(store.db.execute("SELECT key, model FROM events")),
            {"opencode:msg_a": "xai/grok-4.7", "opencode:m3": "minimax/MiniMax-M3"})
        self.assertEqual(sorted(store.models()), ["minimax/MiniMax-M3", "xai/grok-4.7"])
        self.assertEqual(scan(store, self.home).files_read, 0)

    def test_a_cursor_saved_before_v2_reads_the_database_again(self):
        from swe.ledger.scan import scan
        from swe.ledger.store import LedgerStore

        store = LedgerStore(self.home / "ledger.sqlite")
        self.addCleanup(store.db.close)
        st = self.db.stat()
        # What a legacy-only build left: a cursor under the plain path.
        store.set_file_cursor(str(self.db), st.st_ino, st.st_size, st.st_mtime_ns, st.st_size)
        store.db.commit()
        result = scan(store, self.home)
        self.assertEqual(result.records, 2)  # msg_a and the 2.x reply m3
        self.assertIsNotNone(store.file_cursor(str(self.db) + "#v3@w"))


class TailTest(OpencodeV2Case):
    def source(self, sid):
        from swe.remote.tail import OpencodeSource

        return OpencodeSource(self.db, sid)

    def test_v2_rows_stream_in_order_and_once(self):
        src = self.source("ses_new")
        events = src.poll()
        self.assertEqual(
            [e["kind"] for e in events],
            ["user", "thinking", "tool_use", "tool_result", "assistant"],
        )
        self.assertEqual(events[0]["text"], "hello there")
        self.assertEqual(events[2]["tool"]["name"], "shell")
        self.assertEqual(events[3]["text"], "README.md")
        self.assertEqual(src.state()["seq"], 12)
        self.assertEqual(src.poll(), [])

    def test_start_near_end_reads_only_recent_v2_rows(self):
        src = self.source("ses_new")
        src.start_near_end(2)  # rows 7 (assistant) and 12 (idle)
        self.assertEqual(src.state()["seq"], 5)
        self.assertEqual(
            [e["kind"] for e in src.poll()],
            ["thinking", "tool_use", "tool_result", "assistant"],
        )

    def test_start_near_end_counts_undecodable_rows(self):
        # A row whose JSON won't decode still takes its place in the
        # window: the cursor must not fall back to the start.
        conn = self.conn()
        conn.execute("INSERT INTO session_message VALUES ('bad', 'ses_new', 'user', 13, ?, ?, 'not json')",
                     (T0 + 5000, T0 + 5000))
        conn.commit()
        conn.close()
        src = self.source("ses_new")
        src.start_near_end(2)  # rows 12 (idle) and 13 (bad)
        self.assertEqual(src.state()["seq"], 7)

    def test_a_streaming_reply_waits_until_it_completes(self):
        conn = self.conn()
        self.v2_row(conn, "m5", "ses_new", "user", 16, {"text": "next"}, T0 + 5000)
        self.v2_row(conn, "m6", "ses_new", "assistant", 17,
                    _assistant([{"type": "text", "text": "partial"}], finish=None, completed=False), T0 + 5100)
        conn.commit()
        conn.close()
        src = self.source("ses_new")
        src.seq = 12  # the first turn already sent
        self.assertEqual([e["text"] for e in src.poll()], ["next"])
        self.assertEqual(src.state()["seq"], 16)

        conn = self.conn()
        conn.execute("UPDATE session_message SET data = ? WHERE id = 'm6'",
                     (json.dumps(_assistant([{"type": "text", "text": "complete reply"}])),))
        conn.commit()
        conn.close()
        self.assertEqual([e["text"] for e in src.poll()], ["complete reply"])

    def test_skip_to_end_stops_before_a_streaming_reply(self):
        # A journal with no cursor file skips to the end; a reply still
        # streaming there must arrive once it finishes.
        conn = self.conn()
        self.v2_row(conn, "m5", "ses_new", "user", 16, {"text": "next"}, T0 + 5000)
        self.v2_row(conn, "m6", "ses_new", "assistant", 17,
                    _assistant([{"type": "text", "text": "partial"}], finish=None, completed=False), T0 + 5100)
        conn.commit()
        conn.close()
        src = self.source("ses_new")
        src.skip_to_end()
        self.assertEqual(src.state()["seq"], 16)
        self.assertEqual(src.poll(), [])
        conn = self.conn()
        conn.execute("UPDATE session_message SET data = ? WHERE id = 'm6'",
                     (json.dumps(_assistant([{"type": "text", "text": "complete reply"}])),))
        conn.commit()
        conn.close()
        self.assertEqual([e["text"] for e in src.poll()], ["complete reply"])

    def test_a_cursor_from_before_v2_does_not_replay_history(self):
        # A cursor file written by a build that only knew ``part``: its
        # time cursor covers the v2 rows up to that moment.
        src = self.source("ses_new")
        src.restore({"source": "opencode", "cursor": T0 + 1000})
        self.assertEqual(src.poll(), [])
        conn = self.conn()
        self.v2_row(conn, "m5", "ses_new", "user", 16, {"text": "after the upgrade"}, T0 + 5000)
        conn.commit()
        conn.close()
        self.assertEqual([e["text"] for e in src.poll()], ["after the upgrade"])

    def test_legacy_session_tails_its_parts_only(self):
        src = self.source("ses_old")
        self.assertEqual([e["text"] for e in src.poll()], ["fix the old bug", "Fixed the old bug."])
        self.assertEqual(src.poll(), [])


if __name__ == "__main__":
    unittest.main()
