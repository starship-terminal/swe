import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.reports.transcripts import EXPECTED_READER_TOOLS, READER_REGISTRY, read_transcript
from swe.sessions.aggregator import PARSER_REGISTRY
from swe.sessions.models import Session


def _session(tool, sid, path="/work/project", timestamp=1):
    return Session(timestamp=timestamp, agent=tool, path=path, session_id=sid, tool_name=tool)


class _HomeCase(unittest.TestCase):
    """A throwaway HOME and file helpers, with no tests of its own, so
    each test class below runs only its own methods."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def _jsonl(self, relative, records):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        return path


class TranscriptReaderTest(_HomeCase):
    def test_registry_covers_every_session_parser(self):
        parser_tools = {name for name, _parser, _aliases in PARSER_REGISTRY}
        self.assertEqual(parser_tools, EXPECTED_READER_TOOLS)
        self.assertEqual(parser_tools, set(READER_REGISTRY))

    def test_codex_strips_injected_envelopes_but_preserves_semantic_text(self):
        self._jsonl(
            ".codex/sessions/2026/07/30/codex-1.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "codex-1", "cwd": "/work/project"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": (
                            "<recommended_plugins>irrelevant plugin catalog</recommended_plugins>\n"
                            "<environment_context>runtime details</environment_context>\n"
                            "Fix the system parser while preserving {literal: braces}."
                        )}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"command": "python3 -m unittest"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Fixed it; 12 tests pass."}],
                    },
                },
            ],
        )
        transcript = read_transcript(_session("codex", "codex-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.role for m in transcript.messages], ["human", "tool", "assistant"])
        self.assertIn("system parser", transcript.messages[0].text)
        self.assertIn("{literal: braces}", transcript.messages[0].text)
        self.assertNotIn("recommended_plugins", transcript.normalized_text)
        self.assertIn("python3 -m unittest", transcript.messages[1].text)
        self.assertNotIn('{"command"', transcript.messages[1].text)

    def test_an_archived_rollout_still_reads(self):
        # Archiving moves the rollout flat into archived_sessions/; the
        # session stays previewable.
        self._jsonl(
            ".codex/archived_sessions/rollout-2026-10-30T17-40-45-codex-old.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "codex-old", "cwd": "/work/project"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "shelved for later"}],
                    },
                },
            ],
        )
        transcript = read_transcript(
            _session("codex", "rollout-2026-10-30T17-40-45-codex-old")
        )
        self.assertTrue(transcript.readable)
        self.assertIn("shelved for later", transcript.messages[0].text)

    def test_terminal_escapes_in_messages_never_reach_the_preview(self):
        # A stored message that paints, links, writes the clipboard, or
        # clears the screen would run inside the picker's alternate screen.
        self._jsonl(
            ".codex/sessions/2026/07/30/codex-evil.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "codex-evil", "cwd": "/work/project"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": (
                            "safe \x1b]8;;https://evil\x07linked\x1b]8;;\x07 "
                            "\x1b]52;c;AAAA\x07 \x1b[2J \x07done"
                        )}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"command": "rm \x1b[2J -rf"}),
                    },
                },
            ],
        )
        transcript = read_transcript(_session("codex", "codex-evil"))
        self.assertTrue(transcript.readable)
        rendered = "\n".join(m.text for m in transcript.messages)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertIn("safe linked", rendered)
        self.assertIn("done", rendered)

    def test_controls_fuse_before_redaction(self):
        # Controls delete (line-ish ones become newlines) before the
        # redactor runs, so a stray byte inside a value or token cannot
        # split the match and leak a fragment. Words separated by \r
        # stay separate: it becomes a newline.
        from swe.reports.transcripts import _clean_text

        for raw in (
            "API_KEY=secret\x07suffix tail",
            "API_KEY=abc\x1b]8;;u\x07def tail",
            "API_KEY=ab\x1b[31mcdef tail",
            "API_KEY=secret\x1csuffix tail",
            "API_KEY=secret\x0bsuffix tail",
            "API_KEY=secret\x1fsuffix tail",
            "tok sk-" + "a" * 20 + "\x07" + "b" * 20 + " end",
        ):
            out = _clean_text(raw)
            self.assertNotIn("suffix", out)
            self.assertNotIn("secret", out)
            self.assertNotIn("def tail", out)
            self.assertNotIn("cdef", out)
            self.assertNotIn("b" * 20, out)
            self.assertIn("[REDACTED]", out)

        self.assertEqual(_clean_text("warning\rerror"), "warning\nerror")

    def test_unreadable_errors_carry_no_escapes(self):
        # A reader that fails on a hostile filename used to surface the
        # exception (and its bytes) into the picker's preview pane.
        from swe.reports.transcripts import _unreadable

        sess = _session("codex", "x")
        t = _unreadable(sess, "OSError: cannot open 'evil\x1b]8;;u\x07f\x1b[2J'")
        self.assertNotIn("\x1b", t.error)
        self.assertNotIn("\x07", t.error)
        self.assertIn("cannot open 'evilf'", t.error)

    def test_unclosed_envelope_does_not_swallow_adjacent_semantic_markup(self):
        self._jsonl(
            ".codex/sessions/2026/07/30/codex-markup.jsonl",
            [{
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "<recommended_plugins>unclosed\n<task>Preserve this task</task>",
                    }],
                },
            }],
        )
        transcript = read_transcript(_session("codex", "codex-markup"))
        self.assertIn("<task>Preserve this task</task>", transcript.messages[0].text)

    def test_file_edit_tools_preserve_common_structured_filename_fields(self):
        records = []
        expected = []
        for key, filename in (
            ("file_path", "src/file_path.py"),
            ("filePath", "src/filePath.py"),
            ("filename", "src/filename.py"),
        ):
            records.append({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": json.dumps({key: filename, "patch": "omitted"}),
                },
            })
            expected.append(filename)
        self._jsonl(".codex/sessions/2026/07/30/codex-edits.jsonl", records)

        transcript = read_transcript(_session("codex", "codex-edits"))
        self.assertEqual(
            [message.text for message in transcript.messages],
            [f"apply_patch: {filename}" for filename in expected],
        )

    def test_unknown_nonempty_session_id_never_reads_newest_unrelated_file(self):
        older = self._jsonl(
            ".codex/sessions/2026/07/30/known-old.jsonl",
            [{
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "old session"},
            }],
        )
        newer = self._jsonl(
            ".codex/sessions/2026/07/30/known-new.jsonl",
            [{
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "new session"},
            }],
        )
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))

        transcript = read_transcript(_session("codex", "unknown-session"))
        self.assertFalse(transcript.readable)
        self.assertEqual(transcript.messages, [])
        self.assertIn("not found", transcript.error)

    def test_empty_session_id_never_reads_newest_unrelated_file(self):
        # Pi legitimately emits sessions with session_id=""; the mtime
        # fallback used to hand them whichever project transcript was
        # newest and attribute its content to the empty session.
        older = self._jsonl(
            ".pi/agent/sessions/--work-a--/aaa.jsonl",
            [
                {"type": "session", "cwd": "/work/a"},
                {"type": "message", "message": {"role": "user", "content": "project a"}},
            ],
        )
        newer = self._jsonl(
            ".pi/agent/sessions/--work-b--/bbb.jsonl",
            [
                {"type": "session", "cwd": "/work/b"},
                {"type": "message", "message": {"role": "user", "content": "project b"}},
            ],
        )
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))

        transcript = read_transcript(_session("pi", ""))
        self.assertFalse(transcript.readable)
        self.assertEqual(transcript.messages, [])
        self.assertIn("not found", transcript.error)

    def test_claude_drops_local_command_caveat_and_keeps_tool_outcome(self):
        self._jsonl(
            ".claude/projects/-work-project/claude-1.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": (
                        "<local-command-caveat>Caveat: generated locally.</local-command-caveat>\n"
                        "Update the parser."
                    )},
                },
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "I will patch it."},
                        {"type": "tool_use", "name": "Edit", "input": {"path": "parser.py"}},
                    ]},
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "content": "Updated parser.py"}
                    ]},
                },
            ],
        )
        transcript = read_transcript(_session("claude", "claude-1"))
        self.assertEqual([m.role for m in transcript.messages], ["human", "assistant", "tool", "tool"])
        self.assertEqual(transcript.messages[0].text, "Update the parser.")
        self.assertIn("parser.py", transcript.normalized_text)

    def test_secrets_are_redacted_from_human_assistant_and_tool_content(self):
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "c3VwZXItc2VjcmV0LWtleS1tYXRlcmlhbA==\n"
            "-----END PRIVATE KEY-----"
        )
        self._jsonl(
            ".codex/sessions/2026/07/30/codex-secrets.jsonl",
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": (
                            "Use sk-proj-abcdefghijklmnopqrstuv and\n"
                            "Authorization: Bearer bearer-secret-value\n"
                            "export OPENAI_API_KEY='env-secret-value'"
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": (
                            'Configured {"GITHUB_TOKEN": "json-secret-value"}\n'
                            "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "name": "read_file",
                        "output": private_key,
                    },
                },
            ],
        )

        transcript = read_transcript(_session("codex", "codex-secrets"))

        self.assertEqual([m.role for m in transcript.messages], ["human", "assistant", "tool"])
        self.assertGreaterEqual(transcript.normalized_text.count("[REDACTED"), 6)
        for secret in (
            "sk-proj-abcdefghijklmnopqrstuv",
            "bearer-secret-value",
            "env-secret-value",
            "json-secret-value",
            "eyJhbGciOiJIUzI1NiJ9",
            "c3VwZXItc2VjcmV0LWtleS1tYXRlcmlhbA==",
        ):
            self.assertNotIn(secret, transcript.normalized_text)

    def test_secret_redaction_does_not_mask_secret_related_prose(self):
        self._jsonl(
            ".codex/sessions/2026/07/30/codex-prose.jsonl",
            [{
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "Document the token budget, password policy, and placeholder sk-short.",
                },
            }],
        )

        transcript = read_transcript(_session("codex", "codex-prose"))

        self.assertEqual(
            transcript.messages[0].text,
            "Document the token budget, password policy, and placeholder sk-short.",
        )

    def test_digest_is_stable_when_only_secret_values_change(self):
        path = self._jsonl(
            ".codex/sessions/2026/07/30/codex-digest.jsonl",
            [{
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "TOKEN=first-secret-value",
                },
            }],
        )
        first = read_transcript(_session("codex", "codex-digest"))
        path.write_text(
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": "TOKEN=second-secret-value",
                },
            }) + "\n",
            encoding="utf-8",
        )

        second = read_transcript(_session("codex", "codex-digest"))

        self.assertEqual(first.messages[0].text, "TOKEN=[REDACTED]")
        self.assertEqual(first.digest, second.digest)

    def test_empty_droid_and_pi_noop_sessions_are_readable(self):
        self._jsonl(
            ".factory/sessions/project/droid-empty.jsonl",
            [{"type": "session_start", "cwd": "/work/project", "title": "New session"}],
        )
        droid = read_transcript(_session("droid", "droid-empty"))
        self.assertTrue(droid.readable)
        self.assertEqual(droid.messages, [])

        for sid, prompt in (("hello", "hello"), ("version", "version"), ("exit", "/exit")):
            self._jsonl(
                f".pi/agent/sessions/--work-project--/{sid}.jsonl",
                [
                    {"type": "session", "cwd": "/work/project"},
                    {"type": "message", "message": {"role": "user", "content": prompt}},
                    {"type": "message", "message": {"role": "assistant", "content": "ok"}},
                ],
            )
            transcript = read_transcript(_session("pi", sid))
            self.assertEqual(transcript.messages[0].text, prompt)

    def test_opencode_sqlite_reader_preserves_text_and_tool_activity(self):
        db = self.home / ".local/share/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
            "CREATE TABLE part (message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
        )
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m1", "oc-1", 1, json.dumps({"role": "user"})))
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m2", "oc-1", 2, json.dumps({"role": "assistant"})))
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("m1", "oc-1", 1, json.dumps({"type": "text", "text": "Fix tests"})))
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("m2", "oc-1", 2, json.dumps({"type": "reasoning", "text": "The user wants the tests fixed."})))
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("m2", "oc-1", 2, json.dumps({"type": "tool", "tool": "bash", "state": {"input": {"command": "python3 -m unittest"}, "output": "OK"}})))
        conn.commit()
        conn.close()

        transcript = read_transcript(_session("opencode", "oc-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.role for m in transcript.messages], ["human", "tool"])
        self.assertIn("OK", transcript.messages[1].text)

    def test_kilo_sqlite_reader_preserves_text_and_tool_activity(self):
        # Kilo is an opencode fork: same message/part drizzle schema.
        db = self.home / ".local/share/kilo/kilo.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
            "CREATE TABLE part (message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
        )
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m1", "k-1", 1, json.dumps({"role": "user"})))
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m2", "k-1", 2, json.dumps({"role": "assistant"})))
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("m1", "k-1", 1, json.dumps({"type": "text", "text": "Run the suite"})))
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("m2", "k-1", 2, json.dumps({"type": "text", "text": "All green"})))
        conn.commit()
        conn.close()

        transcript = read_transcript(_session("kilo", "k-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual(
            [m.text for m in transcript.messages], ["Run the suite", "All green"])

    def test_cline_3x_reads_session_dir_messages(self):
        # data/sessions/<id>/<id>.messages.json: user text arrives inside
        # <user_input> and thinking blocks are dropped.
        sid = "1789471031317_jp2hv"
        sess_dir = self.home / f".cline/data/sessions/{sid}"
        sess_dir.mkdir(parents=True)
        (sess_dir / f"{sid}.messages.json").write_text(json.dumps({
            "version": 1,
            "sessionId": sid,
            "messages": [
                {"id": "m1", "role": "user", "ts": 1789471031482,
                 "content": [{"type": "text",
                              "text": '<user_input mode="act">hello!</user_input>'}]},
                {"id": "m2", "role": "assistant", "ts": 1789471035698,
                 "content": [{"type": "thinking", "thinking": "simple greeting"},
                             {"type": "text", "text": "Hello! How can I help?"}]},
            ],
        }), encoding="utf-8")

        transcript = read_transcript(_session("cline", sid))
        self.assertTrue(transcript.readable)
        self.assertEqual(
            [m.text for m in transcript.messages],
            ["hello!", "Hello! How can I help?"])
        self.assertEqual(transcript.messages[0].role, "human")

    def test_cline_envelope_unwrap_only_on_fully_wrapped_user_text(self):
        # An assistant block quoting the markup, and a mid-text literal,
        # must not be altered.
        sid = "1789471031317_qw9zz"
        sess_dir = self.home / f".cline/data/sessions/{sid}"
        sess_dir.mkdir(parents=True)
        quoted = 'Use <user_input mode="act">…</user_input> for input'
        literal = "close it with </user_input> like this"
        (sess_dir / f"{sid}.messages.json").write_text(json.dumps({
            "sessionId": sid,
            "messages": [
                {"role": "user", "ts": 1,
                 "content": [{"type": "text", "text": literal}]},
                {"role": "assistant", "ts": 2,
                 "content": [{"type": "text", "text": quoted}]},
            ],
        }), encoding="utf-8")

        transcript = read_transcript(_session("cline", sid))
        self.assertTrue(transcript.readable)
        self.assertEqual(
            [m.text for m in transcript.messages], [literal, quoted])

    def test_cline_mismatched_session_id_is_unreadable(self):
        # A file at the right path but stamped for another session must
        # not be attributed to this one.
        sid = "1789471031317_zzzzz"
        sess_dir = self.home / f".cline/data/sessions/{sid}"
        sess_dir.mkdir(parents=True)
        (sess_dir / f"{sid}.messages.json").write_text(json.dumps({
            "sessionId": "someone_else",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
        }), encoding="utf-8")

        transcript = read_transcript(_session("cline", sid))
        self.assertFalse(transcript.readable)

    def test_cline_messages_not_a_list_is_unreadable(self):
        sid = "1789471031317_bad"
        sess_dir = self.home / f".cline/data/sessions/{sid}"
        sess_dir.mkdir(parents=True)
        (sess_dir / f"{sid}.messages.json").write_text(
            json.dumps({"sessionId": sid, "messages": {"partial": True}}),
            encoding="utf-8")

        transcript = read_transcript(_session("cline", sid))
        self.assertFalse(transcript.readable)

    def test_cline_legacy_tasks_layout_still_reads(self):
        sid = "1777068768553"
        task_dir = self.home / f".cline/data/tasks/{sid}"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text(
            json.dumps([{"role": "user", "content": "legacy task"}]),
            encoding="utf-8")

        transcript = read_transcript(_session("cline", sid))
        self.assertTrue(transcript.readable)
        self.assertEqual(transcript.messages[0].text, "legacy task")

    def test_forge_unwraps_typed_text_envelopes_and_drops_system_messages(self):
        db = self.home / ".forge/.forge.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE conversations (conversation_id TEXT, context TEXT)")
        context = {"messages": [
            {"message": {"text": {"role": "System", "content": "internal policy"}}},
            {"message": {"text": {"role": "User", "content": "Fix the build"}}},
            {"message": {"text": {"role": "Assistant", "content": "Build fixed"}}},
        ]}
        conn.execute("INSERT INTO conversations VALUES (?, ?)", ("forge-1", json.dumps(context)))
        conn.commit()
        conn.close()

        transcript = read_transcript(_session("forge", "forge-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.text for m in transcript.messages], ["Fix the build", "Build fixed"])

    def _devin_db(self, head, nodes):
        db = self.home / ".local/share/devin/cli/sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, main_chain_id INTEGER)")
        conn.execute(
            "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "node_id INTEGER, parent_node_id INTEGER, chat_message TEXT)"
        )
        conn.execute("INSERT INTO sessions VALUES (?, ?)", ("devin-1", head))
        for node_id, parent, message in nodes:
            conn.execute(
                "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message) VALUES (?, ?, ?, ?)",
                ("devin-1", node_id, parent, json.dumps(message)),
            )
        conn.commit()
        conn.close()

    _DEVIN_NODES = [
        (0, None, {"message_id": "m0", "role": "system", "content": "You are Devin"}),
        (1, 0, {"message_id": "m1", "role": "user", "content": "Fix the build",
                "metadata": {"is_user_input": True}}),
        # A streamed reply is stored again as it grows; both versions hang
        # off the same parent and only the last one is on the main chain.
        (2, 1, {"message_id": "m2", "role": "assistant", "content": "Build fi"}),
        (3, 1, {"message_id": "m2", "role": "assistant", "content": "Build fixed",
                "tool_calls": [{"id": "read_1", "name": "read",
                                "arguments": {"file_path": "/work/a.py"}, "kind": "function"}],
                "thinking": {"thinking": "private"}}),
        (4, 3, {"message_id": "m3", "role": "tool", "content": "print('ok')", "tool_call_id": "read_1"}),
    ]

    def test_devin_walks_main_chain_and_labels_tool_results(self):
        self._devin_db(4, self._DEVIN_NODES)

        transcript = read_transcript(_session("devin", "devin-1"))

        self.assertTrue(transcript.readable, transcript.error)
        self.assertEqual(
            [(m.role, m.text) for m in transcript.messages],
            [
                ("human", "Fix the build"),
                ("assistant", "Build fixed"),
                ("tool", "read: /work/a.py"),
                ("tool", "read: print('ok')"),
            ],
        )

    def test_devin_without_a_head_walks_the_chain_from_the_newest_node(self):
        self._devin_db(None, self._DEVIN_NODES)

        transcript = read_transcript(_session("devin", "devin-1"))

        self.assertTrue(transcript.readable, transcript.error)
        self.assertEqual(
            [m.text for m in transcript.messages],
            ["Fix the build", "Build fixed", "read: /work/a.py", "read: print('ok')"],
        )

    def test_devin_without_a_head_never_merges_competing_branches(self):
        """Distinct messages on an abandoned branch must stay out, in any order."""
        self._devin_db(None, [
            (0, None, {"message_id": "m0", "role": "system", "content": "You are Devin"}),
            (1, 0, {"message_id": "m1", "role": "user", "content": "Fix the build"}),
            (2, 1, {"message_id": "m2", "role": "assistant", "content": "Attempt A, abandoned"}),
            (3, 1, {"message_id": "m3", "role": "assistant", "content": "Attempt B"}),
            (4, 2, {"message_id": "m4", "role": "user", "content": "Stray reply on the old branch"}),
            (5, 3, {"message_id": "m5", "role": "user", "content": "Ship it"}),
        ])

        transcript = read_transcript(_session("devin", "devin-1"))

        self.assertEqual(
            [m.text for m in transcript.messages],
            ["Fix the build", "Attempt B", "Ship it"],
        )

    def test_devin_store_missing_is_unreadable_not_an_error(self):
        transcript = read_transcript(_session("devin", "devin-1"))
        self.assertFalse(transcript.readable)
        self.assertIn("Devin store not found", transcript.error)

    def test_missing_source_is_unreadable_not_empty(self):
        transcript = read_transcript(_session("continue", "missing"))
        self.assertFalse(transcript.readable)
        self.assertIn("not found", transcript.error)
        self.assertEqual(transcript.messages, [])


class SecondaryReaderTest(_HomeCase):
    """Readers with bespoke stores: freebuff, grok, cline, gemini,
    copilot, crush, forge, and kilo/opencode sqlite parts."""

    def _json(self, relative, data):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _sqlite(self, relative, statements):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()
        return path

    def test_freebuff_reads_run_state_history(self):
        self._jsonl(
            ".config/manicode/projects/proj/fb-1/log.jsonl",
            [{"role": "user", "content": "jsonl fallback line"}],
        )
        self._json(
            ".config/manicode/projects/proj/fb-1/run-state.json",
            {
                "sessionState": {
                    "mainAgentState": {
                        "messageHistory": [
                            {"role": "user", "content": "state wins"},
                            {"role": "assistant", "content": "ack"},
                        ]
                    }
                }
            },
        )
        transcript = read_transcript(_session("freebuff", "fb-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.text for m in transcript.messages], ["state wins", "ack"])

    def test_freebuff_falls_back_to_log_jsonl(self):
        self._jsonl(
            ".config/manicode/projects/proj/fb-2/log.jsonl",
            [{"role": "user", "content": "from the log"}],
        )
        transcript = read_transcript(_session("freebuff", "fb-2"))
        self.assertTrue(transcript.readable)
        self.assertEqual(transcript.messages[0].text, "from the log")

    def test_freebuff_missing_is_unreadable(self):
        transcript = read_transcript(_session("freebuff", "nope"))
        self.assertFalse(transcript.readable)
        self.assertIn("not found", transcript.error)

    def test_grok_reads_chat_history(self):
        self._jsonl(
            ".grok/sessions/g-1/chat_history.jsonl",
            [{"role": "user", "content": "hi grok"}],
        )
        transcript = read_transcript(_session("grok", "g-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual(transcript.messages[0].text, "hi grok")

    def test_grok_missing_is_unreadable(self):
        transcript = read_transcript(_session("grok", "nope"))
        self.assertFalse(transcript.readable)
        self.assertIn("not found", transcript.error)

    def test_cline_sessions_layout_unwraps_user_input(self):
        self._json(
            ".cline/data/sessions/cl-1/cl-1.messages.json",
            {
                "sessionId": "cl-1",
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "<user_input mode=\"interactive\">wrap me</user_input>"}
                    ], "ts": 1},
                    {"role": "assistant", "content": [{"type": "text", "text": "done"}], "ts": 2},
                    "not a dict",
                ],
            },
        )
        transcript = read_transcript(_session("cline", "cl-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.role for m in transcript.messages], ["human", "assistant"])
        self.assertEqual(transcript.messages[0].text, "wrap me")

    def test_cline_session_id_mismatch_is_unreadable(self):
        self._json(
            ".cline/data/sessions/cl-2/cl-2.messages.json",
            {"sessionId": "other", "messages": [{"role": "user", "content": "x"}]},
        )
        transcript = read_transcript(_session("cline", "cl-2"))
        self.assertFalse(transcript.readable)
        self.assertIn("mismatch", transcript.error)

    def test_cline_tasks_layout_fallback(self):
        self._json(
            ".cline/data/tasks/cl-3/api_conversation_history.json",
            [{"role": "user", "content": "legacy task"}],
        )
        transcript = read_transcript(_session("cline", "cl-3"))
        self.assertTrue(transcript.readable)
        self.assertEqual(transcript.messages[0].text, "legacy task")

    def test_gemini_reads_logs_via_projects_map(self):
        self._json(".gemini/projects.json", {"projects": {"/work/project": "hash123"}})
        self._json(
            ".gemini/tmp/hash123/logs.json",
            [{"role": "user", "content": "gemini says hi"}],
        )
        transcript = read_transcript(_session("gemini", "g-9", path="/work/project"))
        self.assertTrue(transcript.readable)
        self.assertEqual(transcript.messages[0].text, "gemini says hi")

    def test_gemini_without_project_mapping_is_unreadable(self):
        self._json(".gemini/projects.json", {"projects": {}})
        transcript = read_transcript(_session("gemini", "g-9", path="/nowhere"))
        self.assertFalse(transcript.readable)
        self.assertIn("mapping", transcript.error)

    def test_copilot_reads_turns(self):
        self._sqlite(
            ".copilot/session-store.db",
            [
                "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT)",
                "INSERT INTO turns VALUES ('cp-1', 0, 'q one', 'a one')",
                "INSERT INTO turns VALUES ('cp-1', 1, 'q two', 'a two')",
            ],
        )
        transcript = read_transcript(_session("copilot", "cp-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual(
            [(m.role, m.text) for m in transcript.messages],
            [("human", "q one"), ("assistant", "a one"),
             ("human", "q two"), ("assistant", "a two")],
        )

    def test_copilot_tail_reads_only_the_newest_turns(self):
        from swe.reports.transcripts import read_transcript_tail
        self._sqlite(
            ".copilot/session-store.db",
            ["CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT)"]
            + [f"INSERT INTO turns VALUES ('cp-1', {i}, 'q{i}', 'a{i}')" for i in (2, 0, 1)],
        )
        transcript = read_transcript_tail(_session("copilot", "cp-1"), 2)
        self.assertTrue(transcript.readable)
        self.assertEqual([m.text for m in transcript.messages], ["q1", "a1", "q2", "a2"])

    def test_copilot_schema_mismatch_is_unreadable(self):
        self._sqlite(".copilot/session-store.db",
                     ["CREATE TABLE other (id INTEGER)"])
        transcript = read_transcript(_session("copilot", "cp-1"))
        self.assertFalse(transcript.readable)
        self.assertIn("schema error", transcript.error.lower())

    def test_crush_reads_messages_for_matching_project(self):
        self._json(
            ".local/share/crush/projects.json",
            {"projects": [{"path": "/work/project", "data_dir": str(self.home / "crushdata")}]},
        )
        self._sqlite(
            "crushdata/crush.db",
            [
                "CREATE TABLE messages (role TEXT, content TEXT, session_id TEXT)",
                "INSERT INTO messages VALUES ('user', 'crush hi', 'cr-1')",
                "INSERT INTO messages VALUES ('assistant', 'crush ack', 'cr-1')",
            ],
        )
        transcript = read_transcript(_session("crush", "cr-1", path="/work/project"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.text for m in transcript.messages], ["crush hi", "crush ack"])

    def test_crush_tail_reads_only_the_newest_messages(self):
        from swe.reports.transcripts import read_transcript_tail
        self._json(
            ".local/share/crush/projects.json",
            {"projects": [{"path": "/work/project", "data_dir": str(self.home / "crushdata")}]},
        )
        self._sqlite(
            "crushdata/crush.db",
            ["CREATE TABLE messages (role TEXT, content TEXT, session_id TEXT)"]
            + [f"INSERT INTO messages VALUES ('user', 'm{i}', 'cr-1')" for i in range(5)]
            + ["INSERT INTO messages VALUES ('user', 'other', 'cr-2')"],
        )
        transcript = read_transcript_tail(_session("crush", "cr-1", path="/work/project"), 2)
        self.assertEqual([m.text for m in transcript.messages], ["m3", "m4"])

    def test_crush_without_matching_project_is_unreadable(self):
        self._json(".local/share/crush/projects.json", {"projects": []})
        transcript = read_transcript(_session("crush", "cr-1", path="/elsewhere"))
        self.assertFalse(transcript.readable)
        self.assertIn("not found", transcript.error)

    def test_forge_reads_conversation_context(self):
        context = {"messages": [
            {"role": "user", "content": "forge q"},
            {"role": "assistant", "content": "forge a"},
        ]}
        self._sqlite(
            ".forge/.forge.db",
            [
                "CREATE TABLE conversations (conversation_id TEXT, context TEXT)",
                "INSERT INTO conversations VALUES ('fg-1', ?)".replace("?", repr(json.dumps(context))),
            ],
        )
        transcript = read_transcript(_session("forge", "fg-1"))
        self.assertTrue(transcript.readable)
        self.assertEqual([m.text for m in transcript.messages], ["forge q", "forge a"])

    def test_kilo_reads_sqlite_parts(self):
        self._sqlite(
            ".local/share/kilo/kilo.db",
            [
                "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)",
                "CREATE TABLE part (message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)",
                "INSERT INTO message VALUES ('m1', 'ki-1', 1, ?)".replace("?", repr(json.dumps({"role": "user", "content": "kilo q"}))),
                "INSERT INTO part VALUES ('m1', 'ki-1', 2, ?)".replace("?", repr(json.dumps({"type": "text", "text": "kilo a"}))),
            ],
        )
        transcript = read_transcript(_session("kilo", "ki-1"))
        self.assertTrue(transcript.readable)
        texts = [m.text for m in transcript.messages]
        self.assertIn("kilo q", texts)
        self.assertIn("kilo a", texts)


# Keys built at run time, so no scanner reads a credential in this file.
# Each has the length and character mix of the real thing.
_MIXED = "a1B2c3D4e5F6g7H8"
_GLUED_KEYS = {
    "anthropic": "sk-ant-api03-" + "Q" * 30,
    "anthropic admin": "sk-ant-admin01-" + _MIXED * 2,
    "openai project": "sk-proj-" + _MIXED * 3,
    "openai legacy": "sk-" + _MIXED * 3,
    "hex sk": "sk-" + "0123456789abcdef" * 2,
    "github fine-grained": "github_pat_" + "11ABCDEFG0" + "_" + _MIXED * 2,
    "github classic": "ghp_" + "z" * 36,
    "github server": "ghs_" + _MIXED * 2 + "abcd",
    "slack": "xoxb-" + "1234567890-" + _MIXED,
    "google": "AIza" + "SyD-" + _MIXED * 2,
    "aws": "AKIA" + "ABCDEFGH12345678",
    "jwt": ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0In0",
                     "SflKxwRJSMeKKF2QT4fw")),
}


class SecretRedactorBoundaryTest(unittest.TestCase):
    """A credential is masked whatever stands right before it."""

    def redact(self, text):
        from swe.reports.transcripts import _redact_secrets

        return _redact_secrets(text)

    def test_a_key_right_after_a_letter_digit_underscore_or_quote(self):
        for name, key in _GLUED_KEYS.items():
            for before in ("T", "n", "9", "_", '"', "'", " ", "-", "é"):
                with self.subTest(key=name, before=before):
                    self.assertEqual(
                        self.redact(f"see {before}{key} here"),
                        f"see {before}[REDACTED] here")

    def test_a_key_after_an_escape_left_in_the_text(self):
        # A JSON string shown raw keeps ``\n`` as two characters, and a
        # colour code with its ESC gone leaves ``0m``: both put a letter
        # right before the key.
        key = _GLUED_KEYS["anthropic"]
        self.assertEqual(self.redact(f'"line one\\n{key}"'),
                         '"line one\\n[REDACTED]"')
        self.assertEqual(self.redact(f"[0m{key}"), "[0m[REDACTED]")

    def test_a_glued_key_after_words_that_end_in_sk(self):
        # The earlier ``sk-`` in ``task-`` must not use up the match and
        # let the key through. Words joined to the key by hyphens may go
        # with it.
        for name in ("openai legacy", "hex sk", "anthropic", "slack"):
            key = _GLUED_KEYS[name]
            for text in (f"task-list-T{key}", f"risk-ant-T{key} x",
                         f"task-{key}", f"ask-me-task-9{key}."):
                with self.subTest(key=name, text=text[:14]):
                    out = self.redact(text)
                    self.assertNotIn(key, out)
                    self.assertNotIn(key[-12:], out)
                    self.assertIn("[REDACTED]", out)
        anthropic = _GLUED_KEYS["anthropic"]
        self.assertEqual(self.redact(f"risk-ant-T{anthropic} x"),
                         "risk-ant-T[REDACTED] x")

    def test_a_jwt_after_a_word_that_ends_in_sk_goes_whole(self):
        # Its first part alone looks like an ``sk-`` key: 20 letters and
        # digits. Taking that would leave the payload and signature.
        jwt = _GLUED_KEYS["jwt"]
        self.assertEqual(self.redact(f"mask-T{jwt} x"), "mask-T[REDACTED] x")
        self.assertEqual(self.redact(f"desksk-{jwt}"), "desksk-[REDACTED]")

    def test_a_jwt_whose_header_holds_another_eyj(self):
        # ``{"alg":"HS256","kid":"aaa쉬"}`` encodes with a second ``eyJ``
        # four characters from its end, too few for a header of its own.
        # The match starts at the first ``eyJ`` and runs to the dot
        # whatever the header holds.
        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        header = b64('{"alg":"HS256","kid":"aaa쉬"}'.encode())
        self.assertEqual(header.count("eyJ"), 2)
        self.assertEqual(header[-7:-4], "eyJ")
        payload = b64(b'{"sub":"1234567890"}')
        signature = b64(bytes(range(32)))
        early = "eyJ" + "a" * 9 + "eyJ" + "b" * 20
        for head in (header, early):
            jwt = f"{head}.{payload}.{signature}"
            for before in ("T", "9", "task-list-T"):
                with self.subTest(head=head[:12], before=before):
                    self.assertEqual(self.redact(f"{before}{jwt} x"),
                                     f"{before}[REDACTED] x")
            self.assertEqual(self.redact(jwt), "[REDACTED]")

    def test_dotted_names_that_hold_eyj_are_left_alone(self):
        for code in (
            "surveyJsonSchema.properties.questionList",
            "apiKeyJsonSerializer.serializeToString.toByteArray",
            "com.example.monkeyJugglerService.controllers.MainController",
            "publicKeyJwkThumbprint.generated.snapshot",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.redact(code), code)

    def test_an_sk_key_that_holds_another_sk_goes_whole(self):
        # Masking from the second ``sk-`` would leave the key's head.
        key = "sk-proj-abc_Xsk-" + _MIXED * 3
        self.assertEqual(self.redact(f"T{key} x"), "T[REDACTED] x")
        self.assertEqual(self.redact(f"{key} x"), "[REDACTED] x")

    def test_nothing_the_stand_alone_pattern_masks_is_lost(self):
        # A token after a letter can end at a dot, in the middle of a
        # JWT that stands alone further on. The stand-alone pattern runs
        # first, so that JWT is already gone.
        jwt = _GLUED_KEYS["jwt"]
        for text in (f"Tsk-{_MIXED * 2} {jwt}", f"xxAKIA{'A' * 16}-{jwt}",
                     f"task-{_MIXED * 2}/{jwt}.", f"9xoxb-{_MIXED}_{jwt}"):
            with self.subTest(text=text[:16]):
                out = self.redact(text)
                for part in jwt.split("."):
                    self.assertNotIn(part, out)

    def test_ordinary_words_are_left_alone(self):
        for prose in (
            "the task-force-recommendations-document is due",
            "risk-ant and risk-ant-colony-optimization-algorithm",
            "a desk-top, a desk-booking-application-server",
            "ask-me-anything-session-notes and mask-rcnn-resnet-backbone",
            "task-proj-alpha-beta-gamma-delta and disk-usage-analyzer-tool",
            "branch task-2024-quarterly-report-final-version",
            "pod flask-7d9f8b6c5d-x2x9k on disk-0123456789abcdef",
            "task-550e8400-e29b-41d4-a716-446655440000",
            "test_laughs_internationalization and weighs_" + "a" * 24,
            "placeholder sk-short and task-short",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(self.redact(prose), prose)

    def test_a_long_run_with_many_places_to_start_is_left_alone(self):
        # Every ``sk-`` and ``eyJ`` here is a place a token might start,
        # and none does. A run is tried once, from its first character,
        # so it is read a few times over, not once per start.
        n = 20000
        for prose in (
            "task-" * n, ("ask" + "a" * 30 + "-") * n, "risk_x9-" * n,
            "eyJ" * n, ("eyJ" + "a" * 9) * n, "laughs_" * n,
            _MIXED * 2 + "-" + "task-" * n,
            ("TeyJ" + "a" * 10 + ".") * n,
            "T" + "eyJ" * n + ".eyJaaaaaaaa.b",
            ("a" * 12 + ".eyJaaaaaa.") * n,
        ):
            with self.subTest(prose=prose[:16]):
                self.assertEqual(self.redact(prose), prose)

    def test_a_long_run_that_ends_in_a_key_goes_from_its_first_start(self):
        out = self.redact("task-" * 20000 + _MIXED * 2 + " x")
        self.assertEqual(out, "ta[REDACTED] x")
        jwt = _GLUED_KEYS["jwt"]
        out = self.redact("T" + "eyJ" * 20000 + jwt[3:] + " x")
        self.assertEqual(out, "T[REDACTED] x")

if __name__ == "__main__":
    unittest.main()
