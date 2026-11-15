"""Tests for ``swe.sessions.status`` — the ``swe session`` STATUS column.

``~`` is redirected to a per-test temp dir via ``patch.dict`` on ``HOME``;
``now`` is fixed through the ``now`` parameter so the ACTIVE_WINDOW_S
aliveness check never depends on wall clock.
"""

import glob
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from swe.console import strip_ansi

from swe.sessions.models import Session
from swe.sessions.status import (
    ACTIVE,
    ACTIVE_WINDOW_S,
    DONE,
    ERROR,
    FOLLOWUP,
    INTERRUPTED,
    UNKNOWN,
    needs_followup,
    session_status,
    session_statuses,
)

from tests._tempdir import temp_dir
from tests._settled import settle_cache

NOW = 1_700_000_000.0  # seconds
NOW_MS = NOW * 1000


def _session(tool_name, session_id="sid", age_s=0):
    return Session(
        timestamp=NOW_MS - age_s * 1000,
        agent=tool_name,
        path="/tmp/proj",
        title="t",
        session_id=session_id,
        tool_name=tool_name,
    )


def _assistant(text=None, tool_use=False, api_error=False, stop="end_turn"):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.append({"type": "tool_use", "name": "Bash", "input": {}})
    rec = {
        "type": "assistant",
        "message": {"content": content, "stop_reason": stop},
    }
    if api_error:
        rec["isApiErrorMessage"] = True
    return rec


def _user(text=None, tool_result=False, meta=False):
    content = [{"type": "tool_result", "content": "ok"}] if tool_result else (
        [{"type": "text", "text": text}] if text is not None else []
    )
    rec = {"type": "user", "message": {"content": content}}
    if meta:
        rec["isMeta"] = True
    return rec


def _cursor_assistant(text=None, tool_use=False):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.append({"type": "tool_use", "name": "run"})
    return {"role": "assistant", "message": {"content": content}}


def _codex_event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def _codex_item(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


def _codex_msg(role, text):
    block = "output_text" if role == "assistant" else "input_text"
    return _codex_item(
        "message", role=role, content=[{"type": block, "text": text}]
    )


class StatusTestBase(unittest.TestCase):
    def setUp(self):
        self.home = str(temp_dir(self))
        self._patch = patch.dict(os.environ, {"HOME": self.home})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_claude(self, sid, records):
        d = os.path.join(self.home, ".claude", "projects", "-proj")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        with open(path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def _write_claude_session_pid(self, sid, pid, name="1"):
        d = os.path.join(self.home, ".claude", "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".json"), "w") as fh:
            json.dump({"pid": pid, "sessionId": sid}, fh)

    def _write_claude_job(self, sid, state, name="abc"):
        d = os.path.join(self.home, ".claude", "jobs", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump({"sessionId": sid, "state": state}, fh)

    def _write_cursor(self, sid, records):
        d = os.path.join(
            self.home, ".cursor", "projects", "enc",
            "agent-transcripts", sid,
        )
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def _write_devin_db(self, sessions, nodes):
        d = os.path.join(self.home, ".local", "share", "devin", "cli")
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(os.path.join(d, "sessions.db"))
        conn.execute(
            "CREATE TABLE sessions (id TEXT, main_chain_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE message_nodes "
            "(node_id TEXT, parent_node_id TEXT, session_id TEXT, "
            " chat_message TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions (id, main_chain_id) VALUES (?, ?)",
            sessions,
        )
        conn.executemany(
            "INSERT INTO message_nodes "
            "(node_id, parent_node_id, session_id, chat_message) "
            "VALUES (?, ?, ?, ?)",
            nodes,
        )
        conn.commit()
        conn.close()

    def _write_codex(self, stem, records, date=None):
        if date is None:
            m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", stem)
            date = m.groups() if m else ("1970", "01", "01")
        d = os.path.join(self.home, ".codex", "sessions", *date)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, stem + ".jsonl"), "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def _write_opencode_db(self, sessions, messages, parts=(), tool="opencode"):
        d = os.path.join(self.home, ".local", "share", tool)
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(os.path.join(d, f"{tool}.db"))
        conn.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, "
            "session_id TEXT, time_created INTEGER, time_updated INTEGER, "
            "data TEXT)"
        )
        conn.executemany(
            "INSERT INTO session (id, time_created, time_updated) "
            "VALUES (?, ?, ?)",
            sessions,
        )
        conn.executemany(
            "INSERT INTO message "
            "(id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            messages,
        )
        conn.executemany(
            "INSERT INTO part "
            "(id, message_id, session_id, time_created, time_updated, "
            "data) VALUES (?, ?, ?, ?, ?, ?)",
            parts,
        )
        conn.commit()
        conn.close()

    def _write_pi(self, name, records):
        d = os.path.join(self.home, ".pi", "agent", "sessions", "--proj--")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name + ".jsonl")
        with open(path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path


class ClaudeStatusTest(StatusTestBase):
    def test_done_when_last_record_is_plain_end_turn(self):
        self._write_claude("s1", [_assistant("Merged. CI green.")])
        s = _session("claude", "s1")
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_when_last_text_asks_a_question(self):
        self._write_claude(
            "s2", [_assistant("Two options fit. Which one do you want?")]
        )
        s = _session("claude", "s2")
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_error_on_api_error_message(self):
        self._write_claude(
            "s3", [_assistant(api_error=True, stop=None)]
        )
        s = _session("claude", "s3")
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_midturn_active_when_timestamp_is_fresh(self):
        self._write_claude("s4", [_assistant(tool_use=True), _user(tool_result=True)])
        s = _session("claude", "s4", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_midturn_interrupted_when_timestamp_is_stale(self):
        self._write_claude("s4b", [_assistant(tool_use=True), _user(tool_result=True)])
        s = _session("claude", "s4b", age_s=600)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_interrupted_by_user_marker(self):
        self._write_claude(
            "s5",
            [_assistant("Working on it.", stop="tool_use", tool_use=True),
             _user("[Request interrupted by user]")],
        )
        s = _session("claude", "s5", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_live_pid_registry_counts_as_active(self):
        self._write_claude("s6", [_assistant(tool_use=True), _user(tool_result=True)])
        self._write_claude_session_pid("s6", os.getpid())
        s = _session("claude", "s6", age_s=9999)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_dead_pid_is_not_active(self):
        self._write_claude("s6b", [_assistant(tool_use=True), _user(tool_result=True)])
        self._write_claude_session_pid("s6b", 2 ** 22)
        s = _session("claude", "s6b", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_blocked_bg_job_overrides_finished(self):
        self._write_claude("s7", [_assistant("All done here.")])
        self._write_claude_job("s7", "blocked")
        s = _session("claude", "s7", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_failed_bg_job_is_error(self):
        self._write_claude("s7f", [_assistant("All done here.")])
        self._write_claude_job("s7f", "failed")
        s = _session("claude", "s7f", age_s=9999)
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_done_bg_job_falls_through_to_transcript(self):
        self._write_claude("s7d", [_assistant("All done here.")])
        self._write_claude_job("s7d", "done")
        s = _session("claude", "s7d", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_bookkeeping_records_after_end_turn_are_skipped(self):
        self._write_claude(
            "s8",
            [_assistant("Merged. CI green."),
             {"type": "system", "subtype": "turn_duration"},
             {"type": "last-prompt", "prompt": "x"},
             {"type": "mode", "mode": "normal"}],
        )
        s = _session("claude", "s8", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_slash_command_user_record_is_skipped(self):
        self._write_claude(
            "s9",
            [_assistant("Merged. CI green."),
             _user("<command-name>/clear</command-name>")],
        )
        s = _session("claude", "s9", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))


    def test_huge_decisive_record_is_not_lost_to_the_window(self):
        # A >64KiB tool_result lands as the partial first line of the
        # smallest window; _walk_tail must widen until the walk decides.
        big = _user(tool_result=True)
        big["message"]["content"] = [
            {"type": "tool_result", "content": "x" * 100_000}
        ]
        self._write_claude("big1", [_assistant("ok"), big])
        s = _session("claude", "big1", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_huge_decisive_record_with_trailing_bookkeeping(self):
        big = _user(tool_result=True)
        big["message"]["content"] = [
            {"type": "tool_result", "content": "x" * 100_000}
        ]
        self._write_claude(
            "big2", [_assistant("ok"), big, {"type": "last-prompt"}]
        )
        s = _session("claude", "big2", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))


class CursorStatusTest(StatusTestBase):
    def test_done_after_turn_ended_success(self):
        self._write_cursor(
            "c1",
            [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
             _cursor_assistant("Done."),
             {"type": "turn_ended", "status": "success"}],
        )
        s = _session("cursor", "c1", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_after_turn_ended_success_with_question(self):
        self._write_cursor(
            "c2",
            [_cursor_assistant("All wired up. Should I proceed?"),
             {"type": "turn_ended", "status": "success"}],
        )
        s = _session("cursor", "c2", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_error_on_turn_ended_error(self):
        self._write_cursor(
            "c3",
            [_cursor_assistant("trying"),
             {"type": "turn_ended", "status": "error", "error": "boom"}],
        )
        s = _session("cursor", "c3", age_s=9999)
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_interrupted_on_turn_ended_aborted(self):
        self._write_cursor(
            "c4",
            [_cursor_assistant("trying"),
             {"type": "turn_ended", "status": "aborted"}],
        )
        s = _session("cursor", "c4", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_text_scan_stops_at_the_previous_turn_boundary(self):
        # The finished turn produced no text; the earlier turn's question
        # belongs to a turn the user already answered.
        self._write_cursor(
            "c6",
            [_cursor_assistant("Which db do you want?"),
             {"type": "turn_ended", "status": "success"},
             {"role": "user", "message": {"content": [{"type": "text", "text": "postgres"}]}},
             _cursor_assistant(tool_use=True),
             {"type": "turn_ended", "status": "success"}],
        )
        s = _session("cursor", "c6", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_midturn_when_last_record_is_assistant(self):
        self._write_cursor("c5", [_cursor_assistant(tool_use=True)])
        s = _session("cursor", "c5", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))


class DevinStatusTest(StatusTestBase):
    def _mk(self, role, **kw):
        return json.dumps({"role": role, **kw})

    def _write_lock(self, sid, contents):
        d = os.path.join(
            self.home, ".local", "share", "devin", "cli", "session_locks"
        )
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".lock"), "w") as fh:
            fh.write(contents)

    def _tool_head_db(self, sid):
        self._write_devin_db(
            [(sid, "n2")],
            [("n1", None, sid, self._mk("assistant", tool_calls=[{"id": "t"}])),
             ("n2", "n1", sid, self._mk("tool", tool_call_id="t", content="out"))],
        )

    def test_done_when_head_is_plain_assistant(self):
        self._write_devin_db(
            [("d1", "n2")],
            [("n1", None, "d1", self._mk("user", content="hi")),
             ("n2", "n1", "d1", self._mk("assistant", content="All set."))],
        )
        s = _session("devin", "d1", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_active_when_head_is_tool_and_timestamp_fresh(self):
        self._write_devin_db(
            [("d2", "n2")],
            [("n1", None, "d2", self._mk("assistant", tool_calls=[{"id": "t"}])),
             ("n2", "n1", "d2", self._mk("tool", tool_call_id="t", content="out"))],
        )
        s = _session("devin", "d2", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_interrupted_when_tool_head_is_stale(self):
        self._write_devin_db(
            [("d2b", "n2")],
            [("n2", "n1", "d2b", self._mk("tool", tool_call_id="t", content="out"))],
        )
        s = _session("devin", "d2b", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_active_when_assistant_head_has_tool_calls(self):
        self._write_devin_db(
            [("d3", "n1")],
            [("n1", None, "d3", self._mk("assistant", tool_calls=[{"id": "t"}]))],
        )
        s = _session("devin", "d3", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_null_main_chain_falls_back_per_session(self):
        # node_id values are shared across sessions; the filter must hold.
        self._write_devin_db(
            [("d4", None), ("d5", None)],
            [("n1", None, "d5", self._mk("assistant", content="other session?")),
             ("n1", None, "d4", self._mk("assistant", content="All set."))],
        )
        s4 = _session("devin", "d4", age_s=9999)
        s5 = _session("devin", "d5", age_s=9999)
        self.assertEqual(DONE, session_status(s4, now=NOW))
        self.assertEqual(FOLLOWUP, session_status(s5, now=NOW))

    def test_head_is_read_without_decoding_it_in_python(self):
        # A head row carries its whole turn; SQLite pulls out the role,
        # the tool calls and a string content instead of json.loads.
        from swe.sessions import status

        big = "x" * 50_000
        self._write_devin_db(
            [("h1", "n1"), ("h2", "n1"), ("h3", "n1"), ("h4", "n1"),
             ("h5", "n1"), ("h6", None)],
            [("n1", None, "h1", self._mk("assistant", content="All set.", tool_calls=[], log=big)),
             ("n1", None, "h2", self._mk("assistant", content="x", tool_calls=[{"id": "t"}])),
             ("n1", None, "h3", self._mk("assistant", content=[{"type": "text"}], tool_calls={})),
             ("n1", None, "h4", self._mk("tool", content=big)),
             ("n1", None, "h5", self._mk("system", content="sys")),
             ("n9", None, "h6", self._mk("assistant", content="Fallback.", tool_calls=None))],
        )
        conn = status._open_devin_db()
        try:
            with patch.object(status.json, "loads", side_effect=AssertionError("decoded")):
                got = [status._probe_devin(_session("devin", sid), {"devin_conn": conn})
                       for sid in ("h1", "h2", "h3", "h4", "h5", "h6", "none")]
        finally:
            conn.close()
        self.assertEqual(got, [
            ("finished", "All set."), ("midturn", ""), ("finished", ""),
            ("midturn", ""), (None, ""), ("finished", "Fallback."), (None, ""),
        ])

    def test_live_lock_pid_counts_as_active(self):
        self._tool_head_db("d6")
        self._write_lock("d6", str(os.getpid()))
        s = _session("devin", "d6", age_s=9999)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_dead_lock_pid_is_interrupted(self):
        self._tool_head_db("d7")
        self._write_lock("d7", str(2 ** 22))
        s = _session("devin", "d7", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_shared_lock_pid_is_interrupted(self):
        # Devin Desktop hosts every session in one ``devin acp`` process:
        # a pid other locks name proves the app is up, not this turn.
        self._tool_head_db("d11")
        self._write_lock("d11", str(os.getpid()))
        self._write_lock("another-session", str(os.getpid()))
        s = _session("devin", "d11", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_shared_lock_pid_still_active_when_fresh(self):
        self._tool_head_db("d12")
        self._write_lock("d12", str(os.getpid()))
        self._write_lock("another-session", str(os.getpid()))
        s = _session("devin", "d12", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_dead_shared_lock_pid_is_interrupted(self):
        self._tool_head_db("d13")
        self._write_lock("d13", str(2 ** 22))
        self._write_lock("another-session", str(2 ** 22))
        s = _session("devin", "d13", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_single_desktop_lock_pid_is_interrupted(self):
        # A Desktop with one lock left is still the shared host: the
        # Devin.app command path, not the lock count, gives it away.
        self._tool_head_db("d14")
        self._write_lock("d14", str(os.getpid()))
        s = _session("devin", "d14", age_s=9999)
        with patch(
            "swe.sessions.status._pid_command",
            return_value="/Applications/Devin.app/Contents/Resources/"
            "app/extensions/windsurf/devin/bin/devin acp",
        ):
            self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_single_desktop_lock_pid_still_active_when_fresh(self):
        self._tool_head_db("d15")
        self._write_lock("d15", str(os.getpid()))
        s = _session("devin", "d15", age_s=0)
        with patch(
            "swe.sessions.status._pid_command",
            return_value="/Applications/Devin.app/Contents/Resources/"
            "app/extensions/windsurf/devin/bin/devin acp",
        ):
            self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_dead_single_lock_pid_skips_ps(self):
        # Stale locks pile up; a dead pid can't be the host, so it must
        # not cost a ``ps`` fork.
        self._tool_head_db("d16")
        self._write_lock("d16", str(2 ** 22))
        s = _session("devin", "d16", age_s=9999)
        with patch("swe.sessions.status._pid_command") as ps:
            self.assertEqual(INTERRUPTED, session_status(s, now=NOW))
        ps.assert_not_called()

    def test_oversized_lock_pid_is_dead(self):
        # An unrelated lock past pid_t's range must not end the pass.
        self._tool_head_db("d20")
        self._write_lock("d20", str(2 ** 22))
        self._write_lock("garbage", "9999999999\n")
        s = _session("devin", "d20", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_host_pids_skipped_when_no_row_needs_them(self):
        # A fresh transcript proves the turn alive on its own.
        self._tool_head_db("d17")
        self._write_lock("d17", str(os.getpid()))
        s = _session("devin", "d17", age_s=0)
        with patch("swe.sessions.status._devin_host_pids") as hosts:
            self.assertEqual(ACTIVE, session_status(s, now=NOW))
        hosts.assert_not_called()

    def test_host_pids_read_once_per_pass(self):
        self._write_devin_db(
            [("d18", "n2"), ("d19", "n4")],
            [("n1", None, "d18", self._mk("assistant", tool_calls=[{"id": "t"}])),
             ("n2", "n1", "d18", self._mk("tool", tool_call_id="t", content="o")),
             ("n3", None, "d19", self._mk("assistant", tool_calls=[{"id": "t"}])),
             ("n4", "n3", "d19", self._mk("tool", tool_call_id="t", content="o"))],
        )
        rows = [_session("devin", "d18", age_s=9999),
                _session("devin", "d19", age_s=9999)]
        with patch(
            "swe.sessions.status._devin_host_pids", return_value=set()
        ) as hosts:
            self.assertEqual(
                [INTERRUPTED, INTERRUPTED], session_statuses(rows, now=NOW)
            )
        hosts.assert_called_once()

    def test_missing_lock_file_is_interrupted(self):
        self._tool_head_db("d8")
        s = _session("devin", "d8", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_non_integer_lock_contents_is_interrupted(self):
        self._tool_head_db("d9")
        self._write_lock("d9", "not-a-pid\n")
        s = _session("devin", "d9", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_zero_lock_pid_is_interrupted(self):
        # kill(0, 0) probes this process's own group, not a devin pid.
        self._tool_head_db("d10")
        self._write_lock("d10", "0\n")
        s = _session("devin", "d10", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))


class CodexStatusTest(StatusTestBase):
    STEM = (
        "rollout-2026-09-14T11-11-42-"
        "01a09de6-1111-2222-3333-444444444444"
    )

    def test_done_on_task_complete(self):
        self._write_codex(self.STEM, [
            _codex_event("task_started", turn_id="t1"),
            _codex_msg("assistant", "All merged."),
            _codex_event("token_count"),
            _codex_event("task_complete", last_agent_message="All merged."),
        ])
        s = _session("codex", self.STEM, age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_on_task_complete_question(self):
        self._write_codex(self.STEM, [
            _codex_event("task_started", turn_id="t1"),
            _codex_event(
                "task_complete",
                last_agent_message="Which repo should I target?",
            ),
        ])
        s = _session("codex", self.STEM, age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_interrupted_on_turn_aborted(self):
        self._write_codex(self.STEM, [
            _codex_event("task_started", turn_id="t1"),
            _codex_event("turn_aborted", reason="interrupted"),
        ])
        s = _session("codex", self.STEM, age_s=0)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_task_started_is_midturn_resolved_by_freshness(self):
        self._write_codex(self.STEM, [
            _codex_event("task_started", turn_id="t1"),
        ])
        fresh = _session("codex", self.STEM, age_s=0)
        stale = _session("codex", self.STEM, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_bookkeeping_after_task_started_is_still_midturn(self):
        # token_count / item_completed carry no turn state; the reverse
        # walk must pass them and land on task_started.
        self._write_codex(self.STEM, [
            _codex_event("task_started", turn_id="t1"),
            _codex_msg("assistant", "working"),
            _codex_event("token_count"),
            _codex_event("item_completed", item={"type": "AgentMessage"}),
        ])
        fresh = _session("codex", self.STEM, age_s=0)
        stale = _session("codex", self.STEM, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_no_event_msg_falls_back_to_assistant_item(self):
        self._write_codex(self.STEM, [
            {"type": "session_meta", "payload": {"id": "x"}},
            _codex_msg("user", "do it"),
            _codex_msg("assistant", "Done."),
        ])
        s = _session("codex", self.STEM, age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_no_event_msg_function_call_tail_is_midturn(self):
        self._write_codex(self.STEM, [
            _codex_msg("user", "do it"),
            _codex_item(
                "function_call", name="shell", arguments="{}", call_id="c1"
            ),
        ])
        fresh = _session("codex", self.STEM, age_s=0)
        stale = _session("codex", self.STEM, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_reasoning_tail_still_finds_the_prompt_behind_it(self):
        # A reasoning item carries no turn state; the fallback must walk
        # past it to the user message rather than stop at unknown.
        self._write_codex(self.STEM, [
            _codex_msg("user", "do it"),
            _codex_item("reasoning", summary=[]),
        ])
        fresh = _session("codex", self.STEM, age_s=0)
        stale = _session("codex", self.STEM, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_no_event_msg_tool_output_tail_is_midturn(self):
        # The tool answered and the model has not replied yet.
        self._write_codex(self.STEM, [
            _codex_msg("user", "do it"),
            _codex_item(
                "function_call", name="shell", arguments="{}", call_id="c1"
            ),
            _codex_item(
                "function_call_output", call_id="c1", output="ok"
            ),
        ])
        fresh = _session("codex", self.STEM, age_s=0)
        stale = _session("codex", self.STEM, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_session_meta_only_is_unknown(self):
        self._write_codex(self.STEM, [
            {"type": "session_meta", "payload": {"id": "x"}},
        ])
        s = _session("codex", self.STEM, age_s=0)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_missing_rollout_is_unknown(self):
        s = _session("codex", self.STEM, age_s=0)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_date_dir_is_used_directly_without_globbing(self):
        self._write_codex(self.STEM, [
            _codex_event("task_complete", last_agent_message="All merged."),
        ])
        # A different stem under another date dir must never be picked up.
        self._write_codex(
            "rollout-2026-01-01T00-00-00-"
            "ffffffff-0000-0000-0000-000000000000",
            [_codex_event("turn_aborted")],
        )
        real_glob = glob.glob

        def no_codex_glob(pattern, *args, **kw):
            if ".codex" in pattern:
                raise AssertionError("codex probe globbed: " + pattern)
            return real_glob(pattern, *args, **kw)

        s = _session("codex", self.STEM, age_s=9999)
        with patch.object(glob, "glob", side_effect=no_codex_glob):
            self.assertEqual(DONE, session_status(s, now=NOW))

    def test_glob_fallback_finds_a_misplaced_rollout(self):
        # The file's date dir does not match the date in its own stem
        # (it was moved), so the direct path misses and the glob finds it.
        self._write_codex(
            self.STEM,
            [_codex_event("task_complete", last_agent_message="All merged.")],
            date=("2001", "02", "03"),
        )
        s = _session("codex", self.STEM, age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))


def _oc_msg(mid, sid, role, finish=None, created=1):
    data = {"role": role}
    if finish is not None:
        data["finish"] = finish
    return (mid, sid, created, created, json.dumps(data))


def _oc_part(pid, mid, sid, data, created=1):
    return (pid, mid, sid, created, created, json.dumps(data))


class OpencodeStatusTest(StatusTestBase):
    def _write(self, sid, messages, parts=()):
        self._write_opencode_db([(sid, 1, 1)], messages, parts)

    def test_done_on_assistant_stop_with_text_part(self):
        self._write(
            "o1",
            [_oc_msg("m1", "o1", "user", created=1),
             _oc_msg("m2", "o1", "assistant", finish="stop", created=2)],
            [_oc_part("p1", "m2", "o1", {"type": "text", "text": "Done."})],
        )
        s = _session("opencode", "o1", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_on_question_text_part(self):
        self._write(
            "o2",
            [_oc_msg("m1", "o2", "assistant", finish="stop")],
            [_oc_part("p1", "m1", "o2",
                      {"type": "text",
                       "text": "Which model should I use?"})],
        )
        s = _session("opencode", "o2", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_error_on_finish_error(self):
        self._write("o3", [_oc_msg("m1", "o3", "assistant", finish="error")])
        s = _session("opencode", "o3", age_s=9999)
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_tool_calls_finish_is_midturn(self):
        self._write(
            "o4", [_oc_msg("m1", "o4", "assistant", finish="tool-calls")]
        )
        fresh = _session("opencode", "o4", age_s=0)
        stale = _session("opencode", "o4", age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_missing_finish_is_midturn(self):
        self._write("o5", [_oc_msg("m1", "o5", "assistant")])
        fresh = _session("opencode", "o5", age_s=0)
        stale = _session("opencode", "o5", age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_user_head_is_midturn(self):
        self._write(
            "o6",
            [_oc_msg("m1", "o6", "assistant", finish="stop", created=1),
             _oc_msg("m2", "o6", "user", created=2)],
        )
        fresh = _session("opencode", "o6", age_s=0)
        stale = _session("opencode", "o6", age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_finished_text_comes_only_from_the_last_message(self):
        # The earlier assistant message's question part belongs to a turn
        # the user already answered; the last message has no text part.
        self._write(
            "o7",
            [_oc_msg("m1", "o7", "assistant", finish="stop", created=1),
             _oc_msg("m2", "o7", "user", created=2),
             _oc_msg("m3", "o7", "assistant", finish="stop", created=3)],
            [_oc_part("p1", "m1", "o7",
                      {"type": "text",
                       "text": "Which model should I use?"})],
        )
        s = _session("opencode", "o7", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_newest_text_part_wins(self):
        # The newest text part asks a question; an older non-question
        # text part and a non-text part sit behind it. Newest-first wins.
        self._write(
            "o8",
            [_oc_msg("m1", "o8", "assistant", finish="stop")],
            [_oc_part("p1", "m1", "o8",
                      {"type": "text", "text": "All set."}, created=1),
             _oc_part("p2", "m1", "o8",
                      {"type": "step-finish", "reason": "stop"},
                      created=2),
             _oc_part("p3", "m1", "o8",
                      {"type": "text",
                       "text": "Which model should I use?"}, created=3)],
        )
        s = _session("opencode", "o8", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_session_without_messages_is_unknown(self):
        self._write("o9", [])
        s = _session("opencode", "o9", age_s=9999)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_missing_db_is_unknown(self):
        s = _session("opencode", "o10", age_s=9999)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))


class KiloStatusTest(StatusTestBase):
    """Kilo reuses the opencode drizzle schema under ~/.local/share/kilo."""

    def test_done_on_assistant_stop_with_text_part(self):
        self._write_opencode_db(
            [("k1", 1, 1)],
            [_oc_msg("m1", "k1", "user", created=1),
             _oc_msg("m2", "k1", "assistant", finish="stop", created=2)],
            [_oc_part("p1", "m2", "k1", {"type": "text", "text": "Done."})],
            tool="kilo",
        )
        s = _session("kilo", "k1", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_error_on_finish_error(self):
        self._write_opencode_db(
            [("k2", 1, 1)],
            [_oc_msg("m1", "k2", "assistant", finish="error")],
            tool="kilo",
        )
        s = _session("kilo", "k2", age_s=9999)
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_missing_db_is_unknown(self):
        s = _session("kilo", "k3", age_s=9999)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))


class ClineStatusTest(StatusTestBase):
    """Cline 3.x session metadata carries status/pid lifecycle fields."""

    def _write_cline(self, sid, status, pid=None, messages=None):
        d = os.path.join(self.home, ".cline", "data", "sessions", sid)
        os.makedirs(d, exist_ok=True)
        meta = {"session_id": sid, "status": status}
        if pid is not None:
            meta["pid"] = pid
        with open(os.path.join(d, sid + ".json"), "w") as fh:
            json.dump(meta, fh)
        if messages is not None:
            with open(os.path.join(d, sid + ".messages.json"), "w") as fh:
                json.dump({"sessionId": sid, "messages": messages}, fh)

    def _msg(self, role, *texts):
        return {
            "role": role,
            "content": [{"type": "text", "text": t} for t in texts],
        }

    def test_done_on_completed_with_signoff(self):
        self._write_cline(
            "c1", "completed",
            messages=[self._msg("user", "hi"), self._msg("assistant", "Done.")],
        )
        s = _session("cline", "c1", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_on_waiting_with_question(self):
        self._write_cline(
            "c2", "waiting",
            messages=[self._msg("assistant", "Which branch should I use?")],
        )
        s = _session("cline", "c2", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_done_on_waiting_without_question(self):
        self._write_cline(
            "c3", "waiting", messages=[self._msg("assistant", "All set.")]
        )
        s = _session("cline", "c3", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_error_on_failed(self):
        self._write_cline("c4", "failed")
        s = _session("cline", "c4", age_s=9999)
        self.assertEqual(ERROR, session_status(s, now=NOW))

    def test_interrupted_on_canceled_and_paused(self):
        for sid, status in (("c5", "canceled"), ("c6", "paused")):
            self._write_cline(sid, status)
            s = _session("cline", sid, age_s=9999)
            self.assertEqual(INTERRUPTED, session_status(s, now=NOW), status)

    def test_active_on_running_with_live_pid_despite_age(self):
        # started_at never advances, so an old running session needs the
        # pid liveness check to read as active rather than interrupted.
        self._write_cline("c7", "running", pid=os.getpid())
        s = _session("cline", "c7", age_s=9999)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_interrupted_on_running_with_dead_pid(self):
        self._write_cline("c8", "running", pid=2 ** 22)
        s = _session("cline", "c8", age_s=9999)
        self.assertEqual(INTERRUPTED, session_status(s, now=NOW))

    def test_active_on_streaming_with_fresh_timestamp(self):
        self._write_cline("c9", "streaming")
        s = _session("cline", "c9", age_s=0)
        self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_unknown_on_missing_metadata(self):
        s = _session("cline", "gone", age_s=0)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_unknown_on_unrecognized_status(self):
        self._write_cline("c10", "hibernating")
        s = _session("cline", "c10", age_s=9999)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_idle_without_messages_file_is_done(self):
        self._write_cline("c11", "idle")
        s = _session("cline", "c11", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_finished_scans_past_noise_for_last_text(self):
        # Non-dict records and text-less assistant blocks are skipped on
        # the walk back to real text.
        self._write_cline(
            "c12", "waiting",
            messages=[
                self._msg("assistant", "Ship it?"),
                {"role": "assistant",
                 "content": [{"type": "thinking", "thinking": "hmm"}]},
                "not-a-record",
            ],
        )
        s = _session("cline", "c12", age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_answered_question_is_not_followup(self):
        # The user replied after the question, so the earlier assistant
        # text belongs to an answered turn and must not leak through.
        self._write_cline(
            "c12b", "waiting",
            messages=[
                self._msg("assistant", "Ship it?"),
                self._msg("user", "go"),
                {"role": "assistant",
                 "content": [{"type": "thinking", "thinking": "hmm"}]},
            ],
        )
        s = _session("cline", "c12b", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_waiting_with_no_assistant_text_is_done(self):
        self._write_cline("c13", "waiting", messages=[self._msg("user", "hi")])
        s = _session("cline", "c13", age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_unchanged_messages_file_is_not_read_again(self):
        settle_cache(self)
        # The follow-up flag comes from <id>.messages.json alone, so it is
        # cached against that file's signature; the small metadata file
        # is still read every pass, since its status moves on its own.
        from swe.sessions import status

        self._write_cline(
            "c14", "waiting",
            messages=[self._msg("assistant", "Which branch should I use?")],
        )
        s = _session("cline", "c14", age_s=9999)
        reads = []
        real = status._cline_read_text

        def counting(path):
            reads.append(path)
            return real(path)

        with patch.object(status, "_cline_read_text", side_effect=counting):
            self.assertEqual(FOLLOWUP, session_status(s, now=NOW))
            self.assertEqual(FOLLOWUP, session_status(s, now=NOW))
            self.assertEqual(len(reads), 1)
            self._write_cline("c14", "completed")
            self.assertEqual(FOLLOWUP, session_status(s, now=NOW))
            self.assertEqual(len(reads), 1)
            self._write_cline(
                "c14", "waiting",
                messages=[self._msg("assistant", "Pushed and merged.")],
            )
            self.assertEqual(DONE, session_status(s, now=NOW))
            self.assertEqual(len(reads), 2)
            self._write_cline("c14", "running", pid=os.getpid())
            self.assertEqual(ACTIVE, session_status(s, now=NOW))

    def test_a_messages_read_error_is_not_cached(self):
        from swe.sessions import status

        self._write_cline(
            "c15", "waiting",
            messages=[self._msg("assistant", "Which branch should I use?")],
        )
        s = _session("cline", "c15", age_s=9999)
        with patch.object(status, "_cline_read_text", side_effect=OSError("EIO")):
            self.assertEqual(DONE, session_status(s, now=NOW))
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_metadata_is_read_once_per_pass(self):
        from swe.sessions import status

        self._write_cline("c16", "running", pid=os.getpid())
        s = _session("cline", "c16", age_s=9999)
        real = status._cline_meta
        with patch.object(status, "_cline_meta", side_effect=real) as meta:
            self.assertEqual(ACTIVE, session_status(s, now=NOW))
        self.assertEqual(meta.call_count, 1)

    def test_pid_liveness_tolerates_missing_meta(self):
        from swe.sessions.status import _cline_pid_alive

        self.assertFalse(_cline_pid_alive("nonexistent"))


def _pi_msg(role, texts=None, tool_call=False):
    content = [{"type": "text", "text": t} for t in texts or []]
    if tool_call:
        content.append({"type": "toolCall", "id": "c1", "name": "bash"})
    return {"type": "message", "message": {"role": role, "content": content}}


class PiStatusTest(StatusTestBase):
    def test_done_on_trailing_assistant_text(self):
        path = self._write_pi("p1", [
            {"type": "session", "cwd": "/tmp/proj"},
            _pi_msg("user", ["do it"]),
            _pi_msg("assistant", ["Shipped."]),
        ])
        s = _session("pi", path, age_s=9999)
        self.assertEqual(DONE, session_status(s, now=NOW))

    def test_followup_on_trailing_question(self):
        path = self._write_pi("p2", [
            _pi_msg("assistant", ["Which file should I edit?"]),
        ])
        s = _session("pi", path, age_s=9999)
        self.assertEqual(FOLLOWUP, session_status(s, now=NOW))

    def test_midturn_on_trailing_tool_call(self):
        path = self._write_pi("p3", [
            _pi_msg("user", ["do it"]),
            _pi_msg("assistant", tool_call=True),
        ])
        fresh = _session("pi", path, age_s=0)
        stale = _session("pi", path, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_empty_assistant_content_is_midturn(self):
        # Pi appends a placeholder assistant record when a reply starts;
        # one at the tail means the reply never landed.
        path = self._write_pi("p4", [
            _pi_msg("user", ["do it"]),
            _pi_msg("assistant"),
        ])
        fresh = _session("pi", path, age_s=0)
        stale = _session("pi", path, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_tool_result_tail_is_midturn(self):
        path = self._write_pi("p5", [
            _pi_msg("user", ["do it"]),
            _pi_msg("assistant", tool_call=True),
            _pi_msg("toolResult", ["tool output"]),
        ])
        fresh = _session("pi", path, age_s=0)
        stale = _session("pi", path, age_s=600)
        self.assertEqual(ACTIVE, session_status(fresh, now=NOW))
        self.assertEqual(INTERRUPTED, session_status(stale, now=NOW))

    def test_bookkeeping_only_is_unknown(self):
        path = self._write_pi("p6", [
            {"type": "session", "cwd": "/tmp/proj"},
            {"type": "session_info", "name": "mine"},
            {"type": "model_change", "modelId": "x"},
            {"type": "thinking_level_change", "thinkingLevel": "off"},
        ])
        s = _session("pi", path, age_s=0)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_missing_transcript_is_unknown(self):
        s = _session("pi", os.path.join(self.home, "nope.jsonl"), age_s=0)
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))


class MiscStatusTest(StatusTestBase):
    def test_unknown_tool_shows_unknown(self):
        s = _session("gemini", "x")
        self.assertEqual(UNKNOWN, session_status(s, now=NOW))

    def test_statuses_returns_one_label_per_input_in_order(self):
        self._write_claude("m1", [_assistant("Done. Bye.")])
        self._write_cursor(
            "m2", [{"type": "turn_ended", "status": "error"}]
        )
        sessions = [
            _session("claude", "m1", age_s=9999),
            _session("gemini", "x"),
            _session("cursor", "m2", age_s=9999),
        ]
        self.assertEqual(
            [DONE, UNKNOWN, ERROR],
            session_statuses(sessions, now=NOW),
        )


class TailRecordsTest(unittest.TestCase):
    def test_small_file_is_complete(self):
        from swe.sessions.status import _tail_records

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        records, complete = _tail_records(path, 65536)
        self.assertTrue(complete)
        self.assertEqual([{"a": 1}, {"b": 2}], list(records))

    def test_large_file_is_incomplete_and_drops_partial_first_line(self):
        from swe.sessions.status import _tail_records

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"head": "x" * 500}) + "\n")
            fh.write(json.dumps({"tail": 1}) + "\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        records, complete = _tail_records(path, 64)
        self.assertFalse(complete)
        self.assertEqual([{"tail": 1}], list(records))

    def test_window_starting_on_a_line_boundary_keeps_that_record(self):
        # The window opens exactly where the last record starts, so the
        # record before it ends on the byte just outside the window. That
        # record was dropped as a "partial" first line, losing the window's
        # only whole record.
        from swe.sessions.status import _tail_records

        tmp = temp_dir(self)
        last = json.dumps({"tail": 1}) + "\n"
        path = tmp / "s.jsonl"
        path.write_text(json.dumps({"head": "x" * 500}) + "\n" + last)
        records, complete = _tail_records(str(path), len(last))
        self.assertFalse(complete)
        self.assertEqual([{"tail": 1}], list(records))
        # One byte smaller and the window opens inside the record.
        records, _ = _tail_records(str(path), len(last) - 1)
        self.assertEqual([], list(records))

    def test_a_walk_decodes_only_the_records_it_reaches(self):
        from swe.sessions import status

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for i in range(200):
                fh.write(json.dumps({"type": "user", "message": {"content": f"q{i}"}}) + "\n")
            fh.write("not json\n\n")
            fh.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}}) + "\n")
            fh.write("[1, 2]\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        real = json.loads
        with patch.object(status.json, "loads", side_effect=real) as loads:
            self.assertEqual(status._walk_tail(path, status._claude_walk), ("finished", "Done."))
        # The trailing array, then the assistant record; nothing older.
        self.assertEqual(loads.call_count, 2)

    def test_records_walk_newest_first_and_oldest_first_alike(self):
        from swe.sessions.status import _tail_records

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for i in range(5):
                fh.write(json.dumps({"i": i}) + "\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        records, _ = _tail_records(path, 65536)
        newest = reversed(records)
        self.assertEqual(next(newest), {"i": 4})
        self.assertEqual([r["i"] for r in records], [0, 1, 2, 3, 4])
        self.assertEqual([r["i"] for r in newest], [3, 2, 1, 0])
        self.assertEqual([r["i"] for r in reversed(records)], [4, 3, 2, 1, 0])


class StatusLegendTest(unittest.TestCase):
    def test_legend_is_none_when_everything_is_unknown(self):
        from swe.sessions.commands import _status_legend

        self.assertIsNone(_status_legend(["", "", ""]))
        self.assertIsNone(_status_legend([]))

    def test_legend_lists_all_five_glyphs(self):
        from swe.sessions.commands import _status_legend

        line = _status_legend(["done"])
        plain = strip_ansi(line)
        for glyph in ("●", "✓", "?", "✗", "■"):
            self.assertIn(glyph, plain)
        for label in ("active", "done", "followup", "error", "interrupted"):
            self.assertIn(label, plain)

    def test_cmd_session_prints_legend_only_when_a_status_is_known(self):
        from swe.sessions.commands import cmd_session

        sessions = [_session("claude", "l1"), _session("codex", "l2")]
        for statuses, expect_legend in (
            (["done", ""], True),
            (["", ""], False),
        ):
            with patch.dict(os.environ, {"HOME": str(temp_dir(self))}), patch(
                "swe.sessions.commands.get_all_sessions",
                return_value=sessions,
            ), patch(
                "swe.sessions.commands.session_statuses",
                return_value=statuses,
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_session([])
            plain = strip_ansi(buf.getvalue())
            legend_lines = [
                ln for ln in plain.splitlines() if "·" in ln and "active" in ln
            ]
            if expect_legend:
                self.assertEqual(1, len(legend_lines))
                self.assertLess(
                    plain.index(legend_lines[0]), plain.index("LAST ACTIVE")
                )
            else:
                self.assertEqual([], legend_lines)


class NeedsFollowupTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "Should I proceed?": True,
            "Done.": False,
            "Let me know which branch to use.": True,
            "Needs input: pick a colour": True,
            "Blocked: waiting on CI. Nothing needed from you.": False,
            "OK?": False,
            "```\nx?\n```": False,
            "Waiting for your approval before pushing.": True,
        }
        for text, expected in cases.items():
            self.assertEqual(expected, needs_followup(text), text)


if __name__ == "__main__":
    unittest.main()
