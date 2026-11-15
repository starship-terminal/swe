"""Session rows carry the model of the transcript's newest reply (#643)."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.remote import catalog as catalog_mod
from swe.remote import session_model
from swe.sessions.models import Session
from tests._settled import settle_cache


def _session(sid, tool, ts=1000, account_home=""):
    return Session(timestamp=ts, agent=tool, path="/tmp", title="t",
                   session_id=sid, tool_name=tool, account_home=account_home)


def _claude(model, sidechain=False, kind="assistant"):
    return {"type": kind, "isSidechain": sidechain,
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "hi"}]}}


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class _Home(unittest.TestCase):
    """A throwaway $HOME, with the in-memory database answers cleared."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        session_model._db_seen.clear()
        self.addCleanup(session_model._db_seen.clear)


class TestFileHarnesses(_Home):
    def claude_file(self, sid, records):
        path = self.home / ".claude/projects/-tmp" / f"{sid}.jsonl"
        _write_jsonl(path, records)
        return path

    def test_claude_newest_reply_wins(self):
        self.claude_file("c1", [
            _claude("claude-sonnet-4-5"),
            {"type": "user", "message": {"role": "user", "content": "go"}},
            _claude("claude-opus-4-5"),
            {"type": "user", "message": {"role": "user", "content": "more"}},
        ])
        self.assertEqual(session_model.session_models([_session("c1", "claude")]),
                         ["claude-opus-4-5"])

    def test_claude_skips_synthetic_and_sidechain_replies(self):
        self.claude_file("c2", [
            _claude("claude-opus-4-5"),
            _claude("claude-haiku-4-5", sidechain=True),
            _claude("<synthetic>"),
        ])
        self.assertEqual(session_model.session_models([_session("c2", "claude")]),
                         ["claude-opus-4-5"])

    def test_claude_reply_past_the_first_window_is_found(self):
        filler = {"type": "user",
                  "message": {"role": "user", "content": "x" * (40 << 10)}}
        self.claude_file("c3", [_claude("claude-opus-4-5"), filler, filler])
        self.assertEqual(session_model.session_models([_session("c3", "claude")]),
                         ["claude-opus-4-5"])

    def test_no_transcript_or_no_reply_is_unknown(self):
        self.claude_file("c4", [
            {"type": "user", "message": {"role": "user", "content": "hi"}}])
        got = session_model.session_models(
            [_session("c4", "claude"), _session("gone", "claude")])
        self.assertEqual(got, ["", ""])

    def test_codex_newest_turn_context(self):
        sid = "rollout-2026-10-30T10-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
        path = self.home / ".codex/sessions/2026/10/30" / f"{sid}.jsonl"
        _write_jsonl(path, [
            {"type": "session_meta", "payload": {"cwd": "/tmp"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.1-codex"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.2"}},
            {"type": "response_item", "payload": {"type": "message",
                                                  "role": "assistant"}},
        ])
        self.assertEqual(session_model.session_models([_session(sid, "codex")]),
                         ["gpt-5.2"])

    def test_pi_assistant_message(self):
        path = self.home / "pi/session.jsonl"
        _write_jsonl(path, [
            {"type": "session", "cwd": "/tmp"},
            {"type": "message", "message": {"role": "assistant",
                                            "model": "glm-4.6"}},
            {"type": "model_change", "modelId": "other"},
            {"type": "message", "message": {"role": "user", "content": "hi"}},
        ])
        self.assertEqual(
            session_model.session_models([_session(str(path), "pi")]),
            ["glm-4.6"])

    def test_unchanged_transcript_is_read_once(self):
        settle_cache(self)
        path = self.claude_file("c5", [_claude("claude-opus-4-5")])
        session = _session("c5", "claude")
        real = session_model.tail_model
        calls = []

        def counting(*args):
            calls.append(args[0])
            return real(*args)

        with patch.object(session_model, "tail_model", counting):
            self.assertEqual(session_model.session_models([session]),
                             ["claude-opus-4-5"])
            self.assertEqual(session_model.session_models([session]),
                             ["claude-opus-4-5"])
            self.assertEqual(len(calls), 1)
            with open(path, "a") as fh:
                fh.write(json.dumps(_claude("claude-sonnet-4-5")) + "\n")
            self.assertEqual(session_model.session_models([session]),
                             ["claude-sonnet-4-5"])
            self.assertEqual(len(calls), 2)

    def test_harness_without_a_reader_is_unknown(self):
        self.assertEqual(
            session_model.session_models([_session("x", "gemini")]), [""])


class TestDatabaseHarnesses(_Home):
    def opencode_db(self):
        path = self.home / ".local/share/opencode/opencode.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
                                  data TEXT);
            CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT,
                                          seq INTEGER, time_created INTEGER,
                                          data TEXT);
        """)
        return conn

    def test_opencode_newest_row_across_tables(self):
        conn = self.opencode_db()
        conn.execute("INSERT INTO message VALUES ('m1', 's1', 100, ?)", (json.dumps(
            {"role": "assistant", "modelID": "old-model",
             "providerID": "anthropic"}),))
        conn.execute("INSERT INTO session_message VALUES ('m2', 's1', 'assistant',"
                     " 1, 200, ?)", (json.dumps(
                         {"model": {"id": "nvidia/nemotron:free",
                                    "providerID": "openrouter"}}),))
        conn.execute("INSERT INTO message VALUES ('m3', 's2', 300, ?)", (json.dumps(
            {"role": "assistant", "modelID": "claude-opus-4-5",
             "providerID": "anthropic"}),))
        conn.commit()
        conn.close()
        got = session_model.session_models(
            [_session("s1", "opencode"), _session("s2", "opencode"),
             _session("s3", "opencode")])
        self.assertEqual(got, ["openrouter/nvidia/nemotron:free",
                               "anthropic/claude-opus-4-5", ""])

    def test_db_answer_is_kept_until_the_session_moves(self):
        conn = self.opencode_db()
        conn.execute("INSERT INTO message VALUES ('m1', 's1', 100, ?)", (json.dumps(
            {"role": "assistant", "modelID": "a", "providerID": "p"}),))
        conn.commit()
        self.assertEqual(session_model.session_models(
            [_session("s1", "opencode", ts=1)]), ["p/a"])
        conn.execute("INSERT INTO message VALUES ('m2', 's1', 200, ?)", (json.dumps(
            {"role": "assistant", "modelID": "b", "providerID": "p"}),))
        conn.commit()
        conn.close()
        # Same last-activity time: the cached answer stands.
        self.assertEqual(session_model.session_models(
            [_session("s1", "opencode", ts=1)]), ["p/a"])
        self.assertEqual(session_model.session_models(
            [_session("s1", "opencode", ts=2)]), ["p/b"])

    def test_a_long_run_of_housekeeping_rows_pages_past(self):
        path = self.home / ".local/share/devin/cli/sessions.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY,"
                     " session_id TEXT, chat_message TEXT)")
        for model in ["swe-1.5"] + ["compactor"] * 45:
            conn.execute("INSERT INTO message_nodes (session_id, chat_message)"
                         " VALUES ('d1', ?)", (json.dumps(
                             {"role": "assistant",
                              "metadata": {"generation_model": model}}),))
        conn.commit()
        conn.close()
        self.assertEqual(session_model.session_models([_session("d1", "devin")]),
                         ["swe-1.5"])

    def test_opencode_junk_rows_page_past_in_each_table(self):
        conn = self.opencode_db()
        conn.execute("INSERT INTO message VALUES ('m0', 's1', 50, ?)", (json.dumps(
            {"role": "assistant", "modelID": "claude-opus-4-5",
             "providerID": "anthropic"}),))
        for i in range(30):
            conn.execute("INSERT INTO message VALUES (?, 's1', ?, ?)", (
                f"j{i}", 100 + i, json.dumps({"role": "assistant",
                                               "modelID": "unknown"})))
        # And OpenCode 2.x's table: s2 has only session_message rows.
        conn.execute("INSERT INTO session_message VALUES ('n0', 's2',"
                     " 'assistant', 0, 50, ?)", (json.dumps(
                         {"model": {"id": "nvidia/nemotron:free",
                                    "providerID": "openrouter"}}),))
        for i in range(30):
            conn.execute("INSERT INTO session_message VALUES (?, 's2',"
                         " 'assistant', ?, ?, ?)", (
                             f"k{i}", i + 1, 100 + i,
                             json.dumps({"model": {"id": "unknown"}})))
        conn.commit()
        conn.close()
        self.assertEqual(session_model.session_models(
            [_session("s1", "opencode"), _session("s2", "opencode")]),
            ["anthropic/claude-opus-4-5", "openrouter/nvidia/nemotron:free"])

    def test_missing_database_is_unknown(self):
        self.assertEqual(
            session_model.session_models([_session("s1", "opencode")]), [""])


class TestReadOnlyOpen(unittest.TestCase):
    def test_a_path_with_uri_characters_opens(self):
        # A raw "file:{path}" URI ends at "?" or "#" in a folder name.
        import sqlite3
        import tempfile
        from pathlib import Path
        from swe.remote import session_model, tail
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "odd?name#1 %2"
        folder.mkdir()
        db = folder / "x.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (v)")
        conn.commit()
        conn.close()
        ro = session_model._open_ro(str(db))
        self.assertIsNotNone(ro)
        self.assertEqual(ro.execute("SELECT count(*) FROM t").fetchone(), (0,))
        ro.close()
        src = tail.OpencodeSource(db, "s")._open()
        self.assertEqual(src.execute("SELECT count(*) FROM t").fetchone(), (0,))
        src.close()


class TestRows(unittest.TestCase):
    def test_row_carries_model_and_modes(self):
        s = _session("s1", "claude")
        _key, row = catalog_mod.session_to_row(s, "done", "m", {}, None,
                                               "claude-opus-4-5")
        self.assertEqual(row["model"], "claude-opus-4-5")
        self.assertEqual(row["model_modes"], ["tmux", "headless"])

    def test_unknown_model_is_left_out(self):
        _key, row = catalog_mod.session_to_row(
            _session("s1", "opencode"), "done", "m", {})
        self.assertNotIn("model", row)
        # opencode's TUI has no /model <name>: only serve switches it.
        self.assertEqual(row["model_modes"], ["serve"])

    def test_codex_switches_in_tmux_and_appserver(self):
        self.assertEqual(session_model.model_modes("codex"),
                         ["tmux", "appserver"])

    def test_catalog_refresh_joins_models(self):
        sessions = [_session("a", "claude"), _session("b", "codex")]
        with tempfile.TemporaryDirectory() as home:
            cat = catalog_mod.Catalog(
                home=home, machine_id="m",
                fetch_sessions=lambda: sessions,
                status_of=lambda ss: [""] * len(ss),
                model_of=lambda ss: ["claude-opus-4-5", ""],
            )
            rows = {r["id"]: r for r in cat.refresh()}
        self.assertEqual(rows["a"]["model"], "claude-opus-4-5")
        self.assertNotIn("model", rows["b"])

    def test_a_failing_model_read_leaves_rows_without_one(self):
        def boom(_sessions):
            raise RuntimeError("no")

        with tempfile.TemporaryDirectory() as home:
            cat = catalog_mod.Catalog(
                home=home, machine_id="m",
                fetch_sessions=lambda: [_session("a", "claude")],
                status_of=lambda ss: [""] * len(ss), model_of=boom)
            rows = cat.refresh()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("model", rows[0])


if __name__ == "__main__":
    unittest.main()
