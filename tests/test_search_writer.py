"""Write path of the FTS5 session-search index.

``sessions/search_writer.py`` writes the file the SWE app's Swift
indexer writes, so these tests pin the parts the two sides must agree on:
the schema and meta rows, the fenced commits, the read decisions, and the
rows a transcript turns into (``search_decode.py``, checked against the
expectations in the Swift ``SearchIdentifiersTests`` and
``SearchChunkerTests``). Every test runs in a throwaway HOME.
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from swe.sessions import search_decode as decode
from swe.sessions import search_index
from swe.sessions import search_writer as writer
from tests.test_json_output import decode as decode_json, run_swe


def claude_line(kind, text=None, session="sess-1", cwd="/work/proj", ts="2026-10-01T10:00:00.000Z", **extra):
    record = {"type": kind, "sessionId": session, "cwd": cwd, "timestamp": ts, **extra}
    if text is not None:
        record["message"] = {"role": kind, "content": text}
    return json.dumps(record, separators=(",", ":")) + "\n"


def codex_line(kind, payload, ts="2026-10-01T10:00:00.000Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload}) + "\n"


class _Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.projects = self.home / ".claude" / "projects" / "-work-proj"
        self.projects.mkdir(parents=True)
        self.codex = self.home / ".codex" / "sessions" / "2026" / "10" / "01"
        self.codex.mkdir(parents=True)

    def write(self, path: Path, text: str, mode="w"):
        with open(path, mode) as handle:
            handle.write(text)
        return str(path)

    def db(self):
        conn = sqlite3.connect(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        return conn

    def run_pass(self):
        return writer.run_pass(self.home)


class SchemaTests(_Home):
    def test_fresh_index_matches_the_reader_and_swift_schema(self):
        self.write(self.projects / "a.jsonl", claude_line("user", "fix the ledger cursor"))
        self.run_pass()
        conn = self.db()
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["schema_version"], "1")
        self.assertRegex(meta["index_incarnation"], r"^[0-9A-F-]{36}$")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"meta", "sessions", "files", "messages", "messages_fts"} <= tables)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertTrue(search_index.available(self.home))
        hits = search_index.search(self.home, "ledger cursor")
        self.assertEqual([h.session_id for h in hits], ["sess-1"])

    def test_reopen_keeps_the_incarnation(self):
        self.run_pass()
        first = dict(self.db().execute("SELECT key, value FROM meta"))["index_incarnation"]
        self.run_pass()
        self.assertEqual(dict(self.db().execute("SELECT key, value FROM meta"))["index_incarnation"], first)

    def test_a_newer_schema_is_left_alone(self):
        path = search_index.index_path(self.home)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
        conn.commit()
        conn.close()
        before = path.read_bytes()
        with self.assertRaises(writer.IndexUnavailable):
            self.run_pass()
        self.assertEqual(path.read_bytes(), before)

    def test_a_torn_file_is_rebuilt(self):
        path = search_index.index_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"this is not sqlite" * 100)
        self.write(self.projects / "a.jsonl", claude_line("user", "hello torn"))
        self.run_pass()
        self.assertTrue(search_index.available(self.home))

    def test_a_second_python_pass_is_refused_while_one_runs(self):
        lock = writer._Lock(search_index.index_path(self.home).parent / writer.LOCK_FILE)
        with lock:
            with self.assertRaises(writer.IndexBusy):
                self.run_pass()


class ClaudeTests(_Home):
    def test_whole_read_fills_session_and_rows(self):
        path = self.write(self.projects / "a.jsonl", "".join([
            claude_line("user", "<system-reminder>injected</system-reminder>"),
            claude_line("user", "  please fix\n the ledger  "),
            claude_line("assistant", [
                {"type": "text", "text": "Looking at Ledger.swift"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "rg jsonlFiles"}},
            ], ts="2026-10-01T10:05:00.000Z"),
            claude_line("user", [{"type": "tool_result", "content": "src/a.py:1"}]),
        ]))
        self.run_pass()
        conn = self.db()
        session = conn.execute(
            "SELECT agent, session_id, file_path, title, cwd, cwd_key, created_at,"
            " updated_at, message_count FROM sessions").fetchone()
        self.assertEqual(session, (
            "claude", "sess-1", path, "please fix the ledger", "/work/proj", "/work/proj",
            "2026-10-01T10:00:00.000Z", "2026-10-01T10:05:00.000Z", 5))
        rows = conn.execute(
            "SELECT m.role, f.user_text, f.assistant_text, f.tool_text, f.identifiers"
            " FROM messages m JOIN messages_fts f ON f.rowid = m.id ORDER BY m.id").fetchall()
        self.assertEqual([r[0] for r in rows], ["user", "user", "assistant", "tool", "tool"])
        self.assertEqual(rows[3][3], "Bash: rg jsonlFiles")
        self.assertEqual(rows[3][4], "jsonlfiles jsonl files")
        self.assertEqual(rows[4][3], "src/a.py:1")
        st = os.stat(path)
        file_row = conn.execute(
            "SELECT ino, byte_offset, size_bytes, mtime_ms, state FROM files").fetchone()
        self.assertEqual(file_row[:3], (st.st_ino, st.st_size, st.st_size))
        sec, nsec = divmod(st.st_mtime_ns, 1_000_000_000)
        self.assertEqual(file_row[3], (sec + nsec / 1e9) * 1000)
        self.assertEqual(file_row[4], "current")

    def test_explicit_title_wins_wherever_it_lands(self):
        self.write(self.projects / "a.jsonl", "".join([
            claude_line("user", "first prompt"),
            json.dumps({"type": "custom-title", "customTitle": "My name", "sessionId": "sess-1"}) + "\n",
            claude_line("user", "second prompt"),
        ]))
        self.run_pass()
        self.assertEqual(self.db().execute("SELECT title FROM sessions").fetchone()[0], "My name")

    def test_subagent_transcripts_and_hidden_files_are_skipped(self):
        (self.projects / "sess-1" / "subagents").mkdir(parents=True)
        self.write(self.projects / "sess-1" / "subagents" / "agent-x.jsonl", claude_line("user", "sub"))
        self.write(self.projects / ".hidden.jsonl", claude_line("user", "hidden"))
        result = self.run_pass()
        self.assertEqual(result.files, 0)

    def test_an_account_whose_id_is_subagents_is_indexed(self):
        # `~/.swe/accounts/claude/subagents/home`: the folder named
        # `subagents` is the account's id, above the transcript root, so
        # its sessions are indexed like any other home's (#1946).
        account = self.home / ".swe" / "accounts" / "claude" / "subagents" / "home" / "projects" / "-work-acct"
        (account / "sess-acct" / "subagents").mkdir(parents=True)
        session = self.write(account / "sess-acct.jsonl",
                             claude_line("user", "quokka account plan", session="sess-acct"))
        # A real subagent transcript in that account is still left out.
        self.write(account / "sess-acct" / "subagents" / "agent-a1.jsonl",
                   claude_line("user", "wombat delegation chatter", session="sess-acct", isSidechain=True))
        result = self.run_pass()
        self.assertEqual((result.files, result.indexed), (1, 1))
        self.assertEqual([r[0] for r in self.db().execute("SELECT path FROM files")], [session])
        self.assertEqual([h.session_id for h in search_index.search(self.home, "quokka")], ["sess-acct"])
        self.assertEqual(search_index.search(self.home, "wombat"), [])

    def test_only_folders_below_the_transcript_root_count(self):
        root = "/h/.swe/accounts/claude/subagents/home/projects"
        self.assertTrue(writer.should_index(root + "/-p/s.jsonl", root))
        self.assertFalse(writer.should_index(root + "/-p/s/subagents/a.jsonl", root))
        self.assertFalse(writer.should_index(root + "/subagents/a.jsonl", root))
        self.assertFalse(writer.should_index(root + "/-p/subagents/deeper/a.jsonl", root))
        # Outside the root (a linked file's target elsewhere): the walk
        # already decided, by the folders it entered.
        self.assertTrue(writer.should_index("/elsewhere/subagents/a.jsonl", root))
        self.assertTrue(writer.should_index(root + "-other/subagents/a.jsonl", root))
        # A file named `subagents` is no folder.
        self.assertTrue(writer.should_index(root + "/-p/subagents", root))

    def test_unchanged_file_is_current_on_the_next_pass(self):
        self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        self.assertEqual(self.run_pass().indexed, 1)
        second = self.run_pass()
        self.assertEqual((second.indexed, second.current), (0, 1))

    def test_append_continues_the_session(self):
        path = self.projects / "a.jsonl"
        self.write(path, claude_line("user", "first prompt about cursors"))
        self.run_pass()
        conn = self.db()
        session_row = conn.execute("SELECT id FROM sessions").fetchone()[0]
        self.write(path, claude_line("assistant", "reply about offsets",
                                     ts="2026-10-02T09:00:00.000Z"), mode="a")
        self.write(path, claude_line("user", "later prompt"), mode="a")
        self.run_pass()
        rows = conn.execute("SELECT session_row_id, role FROM messages ORDER BY id").fetchall()
        self.assertEqual(rows, [(session_row, "user"), (session_row, "assistant"), (session_row, "user")])
        title, count, updated = conn.execute(
            "SELECT title, message_count, updated_at FROM sessions").fetchone()
        # The appended assistant stamp is the latest seen in that span.
        self.assertEqual((title, count, updated), ("first prompt about cursors", 3, "2026-10-02T09:00:00.000Z"))
        self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_partial_last_line_waits_for_its_newline(self):
        path = self.projects / "a.jsonl"
        whole = claude_line("user", "complete line")
        self.write(path, whole + claude_line("user", "half")[:-10])
        self.run_pass()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT byte_offset FROM files").fetchone()[0], len(whole))
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_rewrite_replaces_the_generation_and_drains_orphans(self):
        path = self.projects / "a.jsonl"
        self.write(path, claude_line("user", "old words") + claude_line("user", "more old words"))
        self.run_pass()
        self.write(path, claude_line("user", "new words"))
        self.run_pass()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0], 1)
        self.assertEqual(search_index.search(self.home, "old"), [])
        self.assertEqual(len(search_index.search(self.home, "new")), 1)

    def test_mid_read_flush_commits_in_pieces(self):
        path = self.write(self.projects / "a.jsonl", "".join(
            claude_line("user", f"prompt number {i} " + "x" * 50) for i in range(20)))
        conn = writer.open_index(search_index.index_path(self.home))
        indexer = writer.Indexer(writer.Store(conn), buffer_budget=200)
        self.assertEqual(indexer.index_file(path, "claude"), "indexed")
        conn.close()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 20)
        self.assertEqual(conn.execute("SELECT message_count FROM sessions").fetchone()[0], 20)
        self.assertEqual(conn.execute("SELECT byte_offset FROM files").fetchone()[0], os.path.getsize(path))

    def test_failures_hold_a_file_out_until_its_stat_moves(self):
        path = self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        store = writer.Store(conn)
        stat = writer.file_stat(path)
        for _ in range(writer.FAILURES_BEFORE_HOLDOUT):
            store.mark_failed(path, stat.mtime_ms)
        self.assertEqual(writer.read_need(store.file_row(path), stat), writer.SKIP)
        os.utime(path, ns=(0, stat.size * 10**9 + 7))
        self.assertEqual(writer.read_need(store.file_row(path), writer.file_stat(path)), writer.WHOLE)


class FenceTests(_Home):
    def test_a_moved_row_rolls_back(self):
        path = self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        store = writer.Store(conn)
        stat = writer.file_stat(path)
        fields = writer.SessionFields("claude", "s", path)
        rows = decode.message_rows([decode.Message("user", "hello")])
        self.assertTrue(store.commit_file(path, None, stat, 10, writer.FRESH, fields, rows))
        # A second writer expecting no row loses: nothing new is written.
        self.assertFalse(store.commit_file(path, None, stat, 20, writer.FRESH, fields, rows))
        self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT byte_offset FROM files").fetchone()[0], 10)

    def test_rows_the_app_wrote_for_other_harnesses_are_untouched(self):
        self.run_pass()
        conn = self.db()
        conn.execute("INSERT INTO sessions(agent, session_id, file_path) VALUES ('cursor', 'c1', '/x')")
        conn.execute("INSERT INTO files(path, byte_offset, mtime_ms, session_row_id) VALUES ('/x', 5, 1, 1)")
        conn.commit()
        self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        self.run_pass()
        self.assertEqual(conn.execute(
            "SELECT byte_offset FROM files WHERE path = '/x'").fetchone()[0], 5)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM sessions WHERE agent = 'cursor'").fetchone()[0], 1)

    def test_a_file_the_app_already_read_is_current(self):
        path = self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        stat = writer.file_stat(path)
        self.run_pass()
        conn = self.db()
        conn.execute("DELETE FROM messages")
        conn.execute("UPDATE files SET ino = NULL")  # Swift never refreshes it
        conn.commit()
        self.assertEqual(self.run_pass().current, 1)
        self.assertEqual(writer.file_stat(path), stat)


class CodexTests(_Home):
    def test_response_items_index_outside_paginated_history(self):
        self.write(self.codex / "rollout-2026-10-01T10-00-00-abc.jsonl", "".join([
            codex_line("session_meta", {"id": "thread-1", "cwd": "/work/c"}),
            codex_line("response_item", {"type": "message", "role": "developer",
                                         "content": [{"type": "input_text", "text": "rules"}]}),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "add dark mode"}]}),
            codex_line("response_item", {"type": "local_shell_call",
                                         "action": {"type": "exec", "command": ["rg", "theme"]}}),
            codex_line("response_item", {"type": "function_call_output", "output": "found it"}),
        ]))
        self.run_pass()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT session_id, cwd, title FROM sessions").fetchone(),
                         ("thread-1", "/work/c", "add dark mode"))
        texts = [r[0] for r in conn.execute(
            "SELECT user_text || assistant_text || tool_text FROM messages_fts ORDER BY rowid")]
        self.assertEqual(texts, ["add dark mode", "tool: rg theme", "found it"])

    def test_paginated_history_reads_completed_items_even_on_append(self):
        path = self.codex / "rollout-2026-10-01T10-00-00-def.jsonl"
        self.write(path, "".join([
            codex_line("session_meta", {"id": "thread-2", "history_mode": "paginated",
                                        "thread_name": "Dark mode"}),
            codex_line("event_msg", {"type": "item_completed",
                                     "item": {"type": "UserMessage", "content": "add dark mode"}}),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "dup"}]}),
        ]))
        self.run_pass()
        self.write(path, "".join([
            codex_line("event_msg", {"type": "item_completed", "item": {
                "type": "CommandExecution", "command": ["ls", "-la"], "aggregated_output": "total 0"}}),
            codex_line("response_item", {"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": "dup reply"}]}),
        ]), mode="a")
        self.run_pass()
        conn = self.db()
        texts = [r[0] for r in conn.execute(
            "SELECT user_text || assistant_text || tool_text FROM messages_fts ORDER BY rowid")]
        self.assertEqual(texts, ["add dark mode", "shell: ls -la", "total 0"])
        self.assertEqual(conn.execute("SELECT title FROM sessions").fetchone()[0], "Dark mode")


class MoreCodexTests(_Home):
    def test_legacy_events_tool_calls_and_forks(self):
        self.write(self.codex / "rollout-2026-10-01T10-00-00-ghi.jsonl", "".join([
            codex_line("session_meta", {"session_id": "thread-3"}),
            codex_line("session_meta", {"id": "parent-thread", "cwd": "/parent"}),
            codex_line("turn_context", {"cwd": "/work/turn"}),
            codex_line("event_msg", {"type": "user_message", "message": "legacy prompt"}),
            codex_line("event_msg", {"type": "agent_message", "message": "legacy reply"}),
            codex_line("event_msg", {"type": "item_completed", "item": {"type": "UserMessage"}}),
            codex_line("event_msg", {"type": "token_count"}),
            codex_line("response_item", {"type": "function_call", "name": "exec_command",
                                         "arguments": '{"cmd": "ls"}'}),
            codex_line("response_item", {"type": "custom_tool_call_output",
                                         "output": {"content": [{"text": "a"}, "b", {"x": 1}]}}),
            codex_line("response_item", {"type": "reasoning"}),
            codex_line("compacted", {}),
            "not json\n",
            "[1, 2]\n",
        ]))
        self.run_pass()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT session_id, cwd, title FROM sessions").fetchone(),
                         ("thread-3", "/work/turn", "legacy prompt"))
        texts = [r[0] for r in conn.execute(
            "SELECT user_text || assistant_text || tool_text FROM messages_fts ORDER BY rowid")]
        self.assertEqual(texts, ["legacy prompt", "legacy reply",
                                 'exec_command: {"cmd": "ls"}', "a\nb"])

    def test_paginated_items_of_every_kind(self):
        self.write(self.codex / "rollout-2026-10-01T10-00-00-jkl.jsonl", "".join([
            codex_line("session_meta", {"id": "thread-4", "history_mode": "paginated"}),
            codex_line("event_msg", {"type": "item_completed", "item": {
                "type": "AgentMessage", "content": [{"type": "output_text", "text": "done"}]}}),
            codex_line("event_msg", {"type": "item_completed", "item": {
                "type": "FileChange", "changes": {"src/a.py": {"unified_diff": "+x"}, "b.py": "bad"}}}),
            codex_line("event_msg", {"type": "item_completed", "item": {"type": "Reasoning"}}),
            codex_line("event_msg", {"type": "item_completed", "item": "bad"}),
        ]))
        self.run_pass()
        texts = [r[0] for r in self.db().execute(
            "SELECT user_text || assistant_text || tool_text FROM messages_fts ORDER BY rowid")]
        self.assertEqual(texts, ["done", "apply_patch: src/a.py", "+x", "apply_patch: b.py"])


class WriterEdgeTests(_Home):
    def test_status_reads_an_index_without_writing(self):
        self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        self.run_pass()
        body = writer.status(self.home)
        self.assertEqual((body["exists"], body["schema_version"], body["sessions"], body["messages"]),
                         (True, "1", 1, 1))
        self.assertEqual(body["files"], {"current": 1})
        self.assertEqual(body["sessions_by_agent"], {"claude": 1})
        self.assertEqual(body["last_pass"]["indexed"], 1)

    def test_a_broken_state_file_reads_as_off(self):
        path = search_index.index_path(self.home).parent / writer.STATE_FILE
        path.parent.mkdir(parents=True)
        path.write_text("[1]")
        self.assertEqual(writer.load_state(self.home), {"enabled": False})
        path.write_text("{")
        self.assertFalse(writer.is_enabled(self.home))

    def test_read_need_rules(self):
        stat = writer.FileStat(ino=5, mtime_ms=10.0, size=100)
        row = writer.FileRow("/p", 5, 50, 9.0, 50, 1, "current", 0, None)
        self.assertEqual(writer.read_need(row, stat), writer.APPEND)
        self.assertEqual(writer.read_need(replace(row, ino=6), stat), writer.WHOLE)
        self.assertEqual(writer.read_need(replace(row, state="due"), stat), writer.WHOLE)
        self.assertEqual(writer.read_need(replace(row, byte_offset=-1), stat), writer.WHOLE)
        self.assertEqual(writer.read_need(replace(row, byte_offset=200), stat), writer.WHOLE)
        self.assertEqual(writer.read_need(replace(row, state="failed", fail_count=1), stat), writer.WHOLE)

    def test_a_rewrite_under_the_same_inode_is_read_whole(self):
        path = self.projects / "a.jsonl"
        self.write(path, claude_line("user", "alpha version"))
        self.run_pass()
        with open(path, "r+") as handle:  # same inode, longer, no newline at the old offset
            handle.write(claude_line("user", "second version, much longer than before"))
        self.run_pass()
        conn = self.db()
        self.assertEqual(search_index.search(self.home, "alpha"), [])
        self.assertEqual(conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0], 1)

    def test_vanished_and_unreadable_files(self):
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        indexer = writer.Indexer(writer.Store(conn))
        self.assertEqual(indexer.index_file(str(self.projects / "gone.jsonl"), "claude"), writer.SKIP)
        path = self.write(self.projects / "locked.jsonl", claude_line("user", "secret"))
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        if os.access(path, os.R_OK):  # root reads anything
            self.skipTest("running as root")
        self.assertEqual(indexer.index_file(path, "claude"), "failed")
        self.assertEqual(conn.execute("SELECT state, fail_count FROM files").fetchone(), ("failed", 1))

    def test_a_flush_that_loses_its_fence_stops_the_read(self):
        path = self.write(self.projects / "a.jsonl", "".join(
            claude_line("user", f"prompt {i} " + "x" * 50) for i in range(10)))
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        store = writer.Store(conn)
        indexer = writer.Indexer(store, buffer_budget=100)
        real = store.commit_file
        store.commit_file = lambda *a, **k: False
        self.assertEqual(indexer.index_file(path, "claude"), "moved")
        store.commit_file = real
        self.assertIsNone(store.file_row(path))

    def test_drain_skips_its_probe_when_nothing_was_orphaned(self):
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        store = writer.Store(conn)
        self.assertEqual(store.drain_orphans(), 0)
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key = 'orphans_pending'").fetchone()[0], "0")
        self.assertEqual(store.drain_orphans(), 0)


class ReviewFixTests(_Home):
    def test_an_off_saved_during_a_pass_stays_off(self):
        self.write(self.projects / "a.jsonl", claude_line("user", "hello"))
        writer.set_enabled(self.home, True)
        calls = []

        def stop():
            if not calls:
                writer.set_enabled(self.home, False)
            calls.append(1)
            return False

        writer.run_pass(self.home, should_stop=stop)
        self.assertFalse(writer.is_enabled(self.home))
        self.assertEqual(writer.load_state(self.home)["last_pass"]["indexed"], 1)
        leftovers = [p.name for p in search_index.index_path(self.home).parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_pass_waits_out_a_brief_status_probe(self):
        import fcntl

        lock_path = search_index.index_path(self.home).parent / writer.LOCK_FILE
        lock_path.parent.mkdir(parents=True)
        holder = open(lock_path, "a+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # The probe lets go during the pass's first retry pause: no timer
        # racing the retry loop, and the pause is the writer's own.
        pauses = []

        def pause(seconds):
            pauses.append(seconds)
            holder.close()

        with mock.patch.object(writer.time, "sleep", side_effect=pause):
            self.assertEqual(self.run_pass().files, 0)
        self.assertEqual(pauses, [writer._Lock.RETRY_S])

    def test_the_outcome_is_saved_before_the_lock_is_released(self):
        seen = []
        real = writer._update_state

        def spy(home, change):
            seen.append(writer.is_running(home))
            return real(home, change)

        with mock.patch.object(writer, "_update_state", spy):
            self.run_pass()
        self.assertEqual(seen, [True])

    def test_a_same_length_edit_before_an_append_is_read_whole(self):
        path = self.projects / "a.jsonl"
        self.write(path, claude_line("user", "alpha words"))
        self.run_pass()
        text = path.read_text().replace("alpha", "omega")
        self.write(path, text + claude_line("user", "appended"))
        self.run_pass()
        self.assertEqual(search_index.search(self.home, "alpha"), [])
        self.assertEqual(len(search_index.search(self.home, "omega")), 1)
        self.assertEqual(self.db().execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_a_row_without_this_sides_print_is_read_whole(self):
        path = self.projects / "a.jsonl"
        self.write(path, claude_line("user", "first"))
        self.run_pass()
        (search_index.index_path(self.home).parent / writer.PRINTS_FILE).unlink()
        self.write(path, claude_line("user", "second"), mode="a")
        self.run_pass()
        conn = self.db()
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0], 2)

    def test_a_large_appended_span_is_read_whole(self):
        path = self.write(self.projects / "a.jsonl", claude_line("user", "first"))
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        indexer = writer.Indexer(writer.Store(conn), buffer_budget=50)
        indexer.index_file(path, "claude")
        session = conn.execute("SELECT id FROM sessions").fetchone()[0]
        self.write(Path(path), claude_line("user", "a much longer second prompt"), mode="a")
        self.assertEqual(indexer.index_file(path, "claude"), "indexed")
        self.assertNotEqual(conn.execute("SELECT id FROM sessions").fetchone()[0], session)

    def test_an_oversized_line_is_skipped_without_losing_the_rest(self):
        big = claude_line("user", "x" * 5000)
        path = self.write(self.projects / "a.jsonl",
                          claude_line("user", "before") + big + claude_line("user", "after"))
        with mock.patch.object(writer, "MAX_LINE_BYTES", 1024):
            self.run_pass()
        texts = [r[0] for r in self.db().execute("SELECT user_text FROM messages_fts ORDER BY rowid")]
        self.assertEqual(texts, ["before", "after"])
        self.assertEqual(self.db().execute("SELECT byte_offset FROM files").fetchone()[0],
                         os.path.getsize(path))

    def test_bad_text_and_deep_nesting_never_stop_a_pass(self):
        self.write(self.projects / "a.jsonl",
                   '{"type":"user","sessionId":"s","message":{"content":"bad \\ud800 surrogate"}}\n'
                   + '{"type":"user","a":' + "[" * 100000 + "]" * 100000 + "}\n")
        self.write(self.projects / "b.jsonl", claude_line("user", "fine", session="s2"))
        result = self.run_pass()
        self.assertEqual((result.indexed, result.failed), (2, 0))
        self.assertEqual(len(search_index.search(self.home, "surrogate")), 1)

    def test_the_hub_loop_keeps_its_last_error(self):
        from swe.remote import hub

        class OneCycle(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        writer.set_enabled(self.home, True)
        path = search_index.index_path(self.home)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '9')")
        conn.commit()
        conn.close()
        hub.HubState._search_index_loop(mock.Mock(home=self.home, _stop=OneCycle()))
        self.assertIn("schema version 9", writer.status(self.home)["last_error"]["message"])
        plain = run_swe(self.home, "session", "index", "--status")
        self.assertIn("The hub's last pass failed", plain.stdout)

    def test_status_of_a_torn_index_fails(self):
        path = search_index.index_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not sqlite at all" * 200)
        result = run_swe(self.home, "session", "index", "--status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Couldn't read the search index", result.stdout)


class DecodeTests(unittest.TestCase):
    """Expectations copied from the Swift suites, so both writers agree."""

    def test_identifier_shadow_text(self):
        cases = {
            "jsonlFiles": "jsonlfiles jsonl files",
            "~/.claude/projects/foo": "claude projects foo",
            "MAX_RETRY": "max retry",
            "Ledger.swift": "ledger swift",
            "resolveTerminalPath": "resolveterminalpath resolve terminal path",
            "JSONParser": "",
            "plainword": "",
            "a.b.c": "",
            "": "",
            "Foo foo FOO": "foo",
            "fix jsonlFiles and MAX_RETRY today": "jsonlfiles jsonl files max retry",
            "xABCdy": "xabcdy ab cdy",
            "a1B2": "a1b2 a1",
        }
        for text, expected in cases.items():
            self.assertEqual(decode.identifier_shadow_text(text), expected, text)
        text = " ".join(f"ident_{i}" for i in range(5000))
        self.assertEqual(len(decode.identifier_shadow_text(text, 4000).split(" ")), 4000)

    def test_chunker(self):
        rows = decode.message_rows([decode.Message("user", "hi")])
        self.assertEqual([r.user_text for r in rows], ["hi"])
        self.assertEqual(decode.message_rows([decode.Message("user", "  \n\t ")]), [])
        tool = decode.message_rows([decode.Message("tool", "x" * 10000)])
        self.assertEqual(len(tool[0].tool_text), decode.TOOL_ROW_LIMIT)
        text = "a" * 7900 + " " + "b" * 100
        self.assertEqual(decode.chunks(text), ["a" * 7900 + " ", "b" * 100])
        text = "a" * 3000 + " " + "b" * 5001
        self.assertEqual([len(c) for c in decode.chunks(text)], [8000, 2])
        words = " ".join(f"word{i}" for i in range(3000))
        pieces = decode.chunks(words)
        self.assertGreaterEqual(len(pieces), 4)
        self.assertTrue(all(decode.utf16_len(p) <= decode.CHUNK_LIMIT for p in pieces))
        self.assertEqual("".join(pieces), words)
        text = "a" * 7998 + "\U0001F600" + "xxx"
        pieces = decode.chunks(text)
        self.assertEqual(pieces, ["a" * 7998 + "\U0001F600", "xxx"])

    def test_injected_turns(self):
        self.assertTrue(decode.is_injected_turn("  <command-name>/clear</command-name>"))
        self.assertTrue(decode.is_injected_turn("[Request interrupted by user]"))
        self.assertFalse(decode.is_injected_turn("<div> is a tag"))
        self.assertFalse(decode.is_injected_turn("please fix"))

    def test_stamps_and_content_shapes(self):
        self.assertEqual(decode.stamp_ms("1970-01-01T00:00:01Z"), 1000.0)
        self.assertIsNone(decode.stamp_ms("2026-10-01T10:00:00"))
        self.assertIsNone(decode.stamp_ms("yesterday"))
        self.assertIsNone(decode.stamp_ms(5))
        single = decode.content_messages("assistant", {"type": "text", "text": "one block"}, None)
        self.assertEqual([(m.role, m.text) for m in single], [("assistant", "one block")])
        mixed = decode.content_messages("user", [
            "plain", {"content": "untyped"}, {"type": "image", "text": "skip"}, 7,
            {"type": "tool_result", "output": [{"text": "out"}]},
            {"type": "function_call", "name": "f", "arguments": {"path": "/x"}},
        ], "t")
        self.assertEqual([(m.role, m.text) for m in mixed],
                         [("user", "plain\nuntyped"), ("tool", "out"), ("tool", "f: /x")])
        self.assertEqual(decode.content_messages(None, "dev text", None), [])
        self.assertEqual(decode.content_messages("user", None, None), [])
        self.assertIsNone(decode.tool_result_text(5))
        self.assertEqual(decode.truncate("a\U0001F600b", 2), "a")
        self.assertEqual(decode.truncate("abc", 2), "ab")

    def test_tool_call_text(self):
        self.assertEqual(decode.tool_call_text("Read", {"file_path": "/a", "command": " "}), "Read: /a")
        self.assertEqual(decode.tool_call_text(None, "raw"), "raw")
        self.assertIsNone(decode.tool_call_text(None, {}))
        self.assertEqual(decode.tool_call_text("x", "y" * 3000), "x: " + "y" * decode.TOOL_ARGUMENT_LIMIT)


class CommandTests(_Home):
    def test_swe_session_index_runs_a_pass_and_turns_the_hub_on(self):
        self.write(self.projects / "a.jsonl", claude_line("user", "hello from the cli"))
        result = run_swe(self.home, "session", "index", "--on", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = decode_json(result)["data"]
        self.assertTrue(data["enabled"])
        self.assertEqual(data["pass"]["indexed"], 1)
        self.assertEqual(data["sessions"], 1)
        data = decode_json(run_swe(self.home, "session", "index", "--off", "--json"))["data"]
        self.assertFalse(data["enabled"])
        self.assertNotIn("pass", data)
        plain = run_swe(self.home, "session", "index")
        self.assertIn("1 session, 1 message indexed", plain.stdout)

    def test_status_never_creates_the_index(self):
        result = run_swe(self.home, "session", "index", "--status", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(decode_json(result)["data"]["exists"])
        self.assertFalse(search_index.index_path(self.home).exists())

    def test_unknown_flag_is_refused(self):
        self.assertEqual(run_swe(self.home, "session", "index", "--bogus").returncode, 1)
        self.assertEqual(run_swe(self.home, "session", "index", "--on", "--off").returncode, 1)


class HubLoopTests(_Home):
    def test_loop_indexes_only_while_enabled(self):
        from swe.remote import hub

        class OneCycle(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True

        self.write(self.projects / "a.jsonl", claude_line("user", "hub indexed this"))
        state = mock.Mock(home=self.home, _stop=OneCycle())
        hub.HubState._search_index_loop(state)
        self.assertFalse(search_index.index_path(self.home).exists())
        writer.set_enabled(self.home, True)
        state._stop = OneCycle()
        hub.HubState._search_index_loop(state)
        self.assertEqual(len(search_index.search(self.home, "hub indexed")), 1)


def _walk_before_1538(root, suffix=".jsonl"):
    """The ``os.walk`` + per-file ``realpath`` walk ``_walk`` replaced."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "subagents")
        for name in sorted(filenames):
            if name.endswith(suffix) and not name.startswith("."):
                found.append(os.path.realpath(os.path.join(dirpath, name)))
    return found


class WalkStatsTest(unittest.TestCase):
    """#1538: ``_walk`` and ``_files_in`` resolved every file with
    ``realpath`` (an lstat per path component). The paths are the index's
    keys, shared with the Swift writer, so they must not change."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        real = self.tmp / "real"
        (real / "proj" / "deep").mkdir(parents=True)
        (real / "proj" / "subagents").mkdir()
        (real / ".hidden").mkdir()
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "linked-dir").mkdir(parents=True)
        for rel in ("a.jsonl", "proj/b.jsonl", "proj/deep/c.jsonl",
                    "proj/subagents/s.jsonl", ".hidden/h.jsonl", "proj/.x.jsonl",
                    "proj/notes.txt"):
            (real / rel).write_text("{}\n")
        (elsewhere / "target.jsonl").write_text("{}\n")
        (elsewhere / "linked-dir" / "d.jsonl").write_text("{}\n")
        (real / "proj" / "link.jsonl").symlink_to(elsewhere / "target.jsonl")
        (real / "proj" / "dirlink").symlink_to(elsewhere / "linked-dir")
        (real / "proj" / "broken.jsonl").symlink_to(self.tmp / "missing.jsonl")
        # The walk starts at a symlink to the tree, as ~/.claude can be.
        self.root = self.tmp / "root-link"
        self.root.symlink_to(real)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_walk_finds_the_same_paths_with_far_fewer_resolves(self):
        expected = _walk_before_1538(self.root)
        real_realpath = os.path.realpath
        with mock.patch("swe.sessions.search_writer.os.path.realpath",
                        side_effect=real_realpath) as realpath:
            found = writer._walk(self.root)
        self.assertEqual(found, expected)
        self.assertIn(os.path.join(real_realpath(self.tmp), "elsewhere", "target.jsonl"), found)
        # The root, plus the two symlinked files; never the plain ones.
        self.assertEqual(realpath.call_count, 3)

    def test_a_missing_root_walks_nothing(self):
        self.assertEqual(writer._walk(self.tmp / "nope"), [])

    def test_files_in_resolves_only_symlinks(self):
        directory = self.root / "proj"
        expected = [os.path.realpath(directory / name)
                    for name in sorted(os.listdir(directory))
                    if not name.startswith(".") and (directory / name).is_file()]
        real_realpath = os.path.realpath
        with mock.patch("swe.sessions.search_writer.os.path.realpath",
                        side_effect=real_realpath) as realpath:
            found = writer._files_in(directory)
        self.assertEqual(found, expected)
        self.assertEqual(realpath.call_count, 2)  # the directory and link.jsonl


class WriterFailureTests(_Home):
    """What the writer does when the disk, SQLite or a source lets it
    down: nothing half-written stays behind and a pass goes on."""

    def indexer(self):
        conn = writer.open_index(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        store = writer.Store(conn)
        return writer.Indexer(store), store, conn

    def test_a_failed_state_write_leaves_the_old_file_and_no_temp(self):
        path = self.home / "state" / "indexer.json"
        writer._write_json(path, {"enabled": True})
        with mock.patch("swe.sessions.search_writer.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                writer._write_json(path, {"enabled": False})
        self.assertEqual(json.loads(path.read_text()), {"enabled": True})
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["indexer.json"])

    def test_emptying_a_missing_or_torn_database(self):
        path = self.home / "search.sqlite"
        writer._empty_database(path)
        self.assertFalse(path.exists())
        path.write_bytes(b"not a database " * 100)
        writer._empty_database(path)
        self.assertFalse(path.exists())

    def test_an_sqlite_without_fts5_or_a_locked_file_is_left_alone(self):
        path = search_index.index_path(self.home)
        with mock.patch.object(writer, "_fts5_available", return_value=False):
            with self.assertRaisesRegex(writer.IndexUnavailable, "no FTS5"):
                writer.open_index(path)
        self.assertTrue(writer._fts5_available(sqlite3.connect(":memory:")))
        broken = mock.MagicMock()
        broken.execute.side_effect = sqlite3.OperationalError("no such module: fts5")
        self.assertFalse(writer._fts5_available(broken))
        with mock.patch.object(writer, "_open_existing",
                               side_effect=sqlite3.OperationalError("database is locked")) as opener:
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                writer.open_index(path)
        self.assertEqual(opener.call_count, 1)

    def test_a_container_read_that_fails_is_held_out(self):
        indexer, store, conn = self.indexer()
        stat = writer.FileStat(ino=None, mtime_ms=1000.0, size=3)

        def bad(row, store):
            raise ValueError("text SQLite can't take")

        self.assertEqual(indexer.index_session("/db#s1", stat, bad), "failed")
        self.assertEqual(conn.execute("SELECT state, fail_count FROM files WHERE path = '/db#s1'").fetchone(),
                         ("failed", 1))
        with mock.patch.object(store, "mark_failed", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(indexer.index_session("/db#s2", stat, bad), "failed")
            path = self.write(self.projects / "a.jsonl", claude_line("user", "hi"))
            with mock.patch.object(indexer, "_read", side_effect=OSError("gone")):
                self.assertEqual(indexer.index_file(path, "claude"), "failed")

    def test_a_subagent_transcript_handed_in_directly_is_never_read(self):
        indexer, _store, conn = self.indexer()
        (self.projects / "s1" / "subagents").mkdir(parents=True)
        path = self.write(self.projects / "s1" / "subagents" / "a.jsonl", claude_line("user", "hi"))
        root = str(self.projects.parent)
        self.assertEqual(indexer.index_file(path, "claude", root=root), writer.SKIP)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_an_unfinished_oversized_last_line_waits(self):
        path = self.projects / "a.jsonl"
        path.write_bytes(claude_line("user", "first").encode() + b"x" * 3000)
        with open(path, "rb") as handle, mock.patch.object(writer, "MAX_LINE_BYTES", 1024):
            lines = list(writer._lines(handle, 0))
        self.assertEqual(len(lines), 1)
        self.assertIn(b"first", lines[0][0])

    def test_stamps_that_do_not_parse_still_bound_the_session(self):
        pending = writer._Pending(path="p", harness="claude", appending=False, row=None,
                                  decoder=decode.Decoder("claude"))
        pending.fold_ts("yesterday")
        pending.fold_ts("today")
        self.assertEqual((pending.first_ts, pending.last_ts), ("yesterday", "today"))
        pending.fold_ts("2026-10-01T10:00:00.000Z")
        self.assertEqual((pending.first_ts, pending.last_ts),
                         ("2026-10-01T10:00:00.000Z", "2026-10-01T10:00:00.000Z"))

    def test_cursor_meta_and_chat_index_edges(self):
        meta = self.home / "meta.json"
        for text in ("{broken", "[1]"):
            meta.write_text(text)
            self.assertIsNone(writer.cursor_meta_fields(str(meta)))
        self.assertIsNone(writer.cursor_meta_fields(str(self.home / "missing.json")))
        chats = self.home / "chats"
        self.assertEqual(writer.cursor_chat_meta_index(chats), {})
        (chats / "b-ws" / "c1").mkdir(parents=True)
        (chats / "b-ws" / "c1" / "meta.json").write_text("{}")
        (chats / "a-ws" / "c1").mkdir(parents=True)
        (chats / "a-ws" / "c1" / "meta.json").write_text("{}")
        (chats / "a-ws" / "c2").mkdir()
        (chats / "loose.txt").write_text("x")
        self.assertEqual(writer.cursor_chat_meta_index(chats),
                         {"c1": str(chats / "a-ws" / "c1" / "meta.json")})

    def test_status_of_an_index_sqlite_cannot_open(self):
        path = search_index.index_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        with mock.patch("swe.sessions.search_writer.sqlite3.connect",
                        side_effect=sqlite3.OperationalError("unable to open database file")):
            body = writer.status(self.home)
        self.assertEqual(body["error"], "unable to open database file")
        self.assertFalse(body["exists"])


if __name__ == "__main__":
    unittest.main()
