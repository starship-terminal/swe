"""The hook run log: the writer, the transcript backfill, ``swe hooks stats``.

Everything runs against throwaway homes. Fixture events are stamped
relative to now, so "the last 7 days" holds whenever the suite runs.
"""

import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from swe import paths
from swe.hooks import commands as hooks_commands
from swe.hooks import events, shim_runtime, transcripts
from swe.hooks import stats_commands

from tests._tempdir import temp_dir

HOUR, DAY = 3600, 86400


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _line(now, ago, hook, decision, **extra) -> dict:
    row = {"v": 1, "ts": now - ago, "hook": hook, "harness": "claude",
           "event": "Stop", "session_id": "s1", "cwd": "/w/app",
           "repo": "/w/app", "git_branch": "main", "decision": decision,
           "reason": "", "duration_ms": 10, "exit_code": 0,
           "stop_hook_active": False}
    row.update(extra)
    return row


def _write_log(home: Path, rows: list, name: str = "events.jsonl") -> Path:
    path = paths.hook_events_file_for(home).with_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _run(home: Path, *args: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with mock.patch.object(Path, "home", staticmethod(lambda: home)), \
            redirect_stdout(out), redirect_stderr(err):
        code = hooks_commands.cmd_hooks(list(args))
    return code, _plain(out.getvalue()), err.getvalue()


def _fixture_rows(now: float) -> list:
    return [
        # prose-guard: 4 runs in the window, 2 blocks, 1 recovered.
        _line(now, 1 * HOUR, "prose-guard", "block",
              reason="done-followups: missing Follow-ups", exit_code=2),
        _line(now, 1 * HOUR - 60, "prose-guard", "allow",
              stop_hook_active=True),
        _line(now, 2 * DAY, "prose-guard", "block",
              reason="done-followups: missing Follow-ups", exit_code=2),
        _line(now, 3 * DAY, "prose-guard", "allow"),
        # no-sleep-poll: 3 PreToolUse runs, 1 block.
        _line(now, 5 * HOUR, "no-sleep-poll", "block",
              event="PreToolUse:Bash", reason="Do not poll with sleep",
              exit_code=2, session_id="s2"),
        _line(now, 6 * HOUR, "no-sleep-poll", "allow",
              event="PreToolUse:Bash", session_id="s2"),
        _line(now, 6 * DAY, "no-sleep-poll", "warn",
              event="PreToolUse:Bash", reason="sleep inside a loop",
              session_id="s2"),
        # A cursor copy of a shared hook, and an error.
        _line(now, 1 * DAY, "prose-guard", "error", harness="cursor",
              reason="exit 1", exit_code=1, session_id="s3"),
        # Outside the 7 day window.
        _line(now, 9 * DAY, "prose-guard", "block", reason="old",
              exit_code=2),
    ]


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_HOOK_LOG", None)

    def _lines(self) -> list:
        path = paths.hook_events_file_for(self.home)
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_path_matches_the_paths_helper(self):
        self.assertEqual(
            Path(shim_runtime.events_path(str(self.home))),
            paths.hook_events_file_for(self.home))

    def test_writes_one_line_with_every_field(self):
        ok = shim_runtime.record(
            "prose-guard", "block", "  first line\nsecond line",
            harness="claude", event="Stop", session_id="abc",
            cwd=str(self.home), duration_ms=12, exit_code=2,
            stop_hook_active=False)
        self.assertTrue(ok)
        (row,) = self._lines()
        self.assertEqual(
            sorted(row),
            sorted(["v", "ts", "hook", "harness", "event", "session_id",
                    "cwd", "repo", "git_branch", "decision", "reason",
                    "duration_ms", "exit_code", "stop_hook_active"]))
        self.assertEqual(row["reason"], "first line")
        self.assertEqual(row["decision"], "block")
        self.assertIs(row["stop_hook_active"], False)
        self.assertEqual(row["v"], 1)

    def test_reason_is_one_line_and_capped(self):
        shim_runtime.record("h", "block", "x" * 5000 + "\nsecret body")
        (row,) = self._lines()
        self.assertEqual(len(row["reason"]), shim_runtime.REASON_MAX)
        self.assertNotIn("secret", json.dumps(row))

    def test_repo_and_branch_come_from_dot_git(self):
        repo = self.home / "proj"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n")
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        shim_runtime.record("h", "allow", cwd=str(sub))
        (row,) = self._lines()
        self.assertEqual(row["repo"], str(repo))
        self.assertEqual(row["git_branch"], "feat/x")

    def test_worktree_dot_git_file_and_detached_head(self):
        real = self.home / "main" / ".git" / "worktrees" / "wt"
        real.mkdir(parents=True)
        (real / "HEAD").write_text("0123456789abcdef\n")
        wt = self.home / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {real}\n")
        self.assertEqual(shim_runtime.repo_info(str(wt)),
                         (str(wt), "0123456"))
        self.assertEqual(shim_runtime.repo_info(None), (None, None))
        self.assertEqual(shim_runtime.repo_info("/"), (None, None))

    def test_off_switch(self):
        with mock.patch.dict(os.environ, {"SWE_HOOK_LOG": "0"}):
            self.assertFalse(shim_runtime.record("h", "allow"))
        self.assertFalse(paths.hook_events_file_for(self.home).exists())

    def test_fails_open_when_the_folder_is_a_file(self):
        hooks = paths.hook_events_file_for(self.home).parent
        hooks.parent.mkdir(parents=True)
        hooks.write_text("in the way")
        self.assertFalse(shim_runtime.record("h", "block", "why"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory modes")
    def test_fails_open_when_the_folder_is_not_writable(self):
        folder = paths.hook_events_file_for(self.home).parent
        folder.mkdir(parents=True)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        self.assertFalse(shim_runtime.record("h", "block", "why"))
        self.assertEqual(os.listdir(folder), [])

    def test_shim_result_is_unchanged_when_the_log_is_unwritable(self):
        folder = paths.hook_events_file_for(self.home).parent
        folder.parent.mkdir(parents=True)
        folder.write_text("in the way")
        script = self.home / "blocker.py"
        script.write_text(
            "import sys\nsys.stderr.write('stop it\\nmore\\n')\n"
            "sys.exit(2)\n")
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdin", StringIO("{}")), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = shim_runtime.main(["--contract", '{"harness": "claude"}',
                                    "--", str(script)])
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue(), "stop it\nmore\n")

    def test_rotation_keeps_one_older_generation(self):
        path = paths.hook_events_file_for(self.home)
        with mock.patch.object(shim_runtime, "EVENTS_CAP_BYTES", 600):
            for i in range(20):
                shim_runtime.record(f"h{i}", "allow", "r" * 40)
        rotated = path.with_name("events.jsonl.1")
        self.assertTrue(rotated.exists())
        self.assertFalse(path.with_name("events.jsonl.2").exists())
        self.assertLess(path.stat().st_size, 600 + 400)
        names = [json.loads(x)["hook"] for p in (rotated, path)
                 for x in p.read_text().splitlines()]
        self.assertEqual(names, sorted(names, key=lambda n: int(n[1:]))[
            -len(names):])
        self.assertEqual(names[-1], "h19")
        # Both generations are read back, oldest first.
        read = events.read_log(self.home)
        self.assertEqual([e["hook"] for e in read], names)

    def test_file_is_private(self):
        shim_runtime.record("h", "allow")
        path = paths.hook_events_file_for(self.home)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ShimLoggingTest(unittest.TestCase):
    """The shim logs each run without a change to the script."""

    def setUp(self):
        self.home = temp_dir(self)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_HOOK_LOG", None)

    def _shim(self, body: str, payload: dict, contract=None):
        script = self.home / "prose-guard.py"
        script.write_text(body)
        with mock.patch("sys.stdin", StringIO(json.dumps(payload))), \
                mock.patch("sys.stdout", StringIO()), \
                mock.patch("sys.stderr", StringIO()):
            rc = shim_runtime.main([
                "--contract", json.dumps(contract or {"harness": "claude"}),
                "--", str(script)])
        rows = events.read_log(self.home)
        return rc, rows

    def test_exit_two_is_a_block_with_the_first_stderr_line(self):
        rc, rows = self._shim(
            "import sys\nsys.stderr.write('done-followups: no section\\n"
            "the rest of a long reply\\n')\nsys.exit(2)\n",
            {"hook_event_name": "Stop", "session_id": "s9",
             "cwd": str(self.home), "stop_hook_active": False})
        self.assertEqual(rc, 2)
        (row,) = rows
        self.assertEqual(row["hook"], "prose-guard")
        self.assertEqual(row["decision"], "block")
        self.assertEqual(row["reason"], "done-followups: no section")
        self.assertEqual(row["event"], "Stop")
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["session_id"], "s9")
        self.assertEqual(row["exit_code"], 2)
        self.assertIs(row["stop_hook_active"], False)
        self.assertIsInstance(row["duration_ms"], int)

    def test_stop_hook_active_marks_the_accepted_rewrite(self):
        _, rows = self._shim(
            "import sys\nsys.stderr.write('no\\n')\nsys.exit(2)\n",
            {"hook_event_name": "Stop", "session_id": "s9",
             "stop_hook_active": False})
        rc, rows = self._shim(
            "pass\n", {"hook_event_name": "Stop", "session_id": "s9",
                       "stop_hook_active": True})
        self.assertEqual(rc, 0)
        self.assertEqual([r["decision"] for r in rows], ["block", "allow"])
        self.assertIs(rows[1]["stop_hook_active"], True)
        events.mark_recovered(rows)
        stats = events.build_stats(rows)["hooks"][0]
        self.assertEqual(stats["recovered"], 1)

    def test_a_gemini_ask_is_logged_as_the_deny_the_harness_gets(self):
        ask = ("import json\nprint(json.dumps({'hookSpecificOutput': "
               "{'permissionDecision': 'ask'}}))\n")
        gemini = {"harness": "gemini", "schema": "grouped",
                  "must_print_json": True}
        _, rows = self._shim(ask, {"hook_event_name": "BeforeTool"}, gemini)
        self.assertEqual(rows[-1]["decision"], "block")
        # Claude has a real "ask": the run is not a block there.
        _, rows = self._shim(ask, {"hook_event_name": "PreToolUse"})
        self.assertEqual(rows[-1]["decision"], "allow")

    def test_tool_events_carry_the_tool_name(self):
        _, rows = self._shim(
            "pass\n", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "echo hunter2"}})
        (row,) = rows
        self.assertEqual(row["event"], "PreToolUse:Bash")
        self.assertNotIn("hunter2", json.dumps(row))

    def test_warn_error_and_json_block(self):
        _, warn = self._shim(
            "import sys\nsys.stderr.write('heads up\\n')\n",
            {"hook_event_name": "Stop"})
        self.assertEqual((warn[-1]["decision"], warn[-1]["reason"]),
                         ("warn", "heads up"))
        _, err = self._shim("import sys\nsys.exit(1)\n",
                            {"hook_event_name": "Stop"})
        self.assertEqual((err[-1]["decision"], err[-1]["reason"]),
                         ("error", "exit 1"))
        _, blocked = self._shim(
            "import json\nprint(json.dumps({'decision': 'block', "
            "'reason': 'rule-x: no'}))\n", {"hook_event_name": "Stop"})
        self.assertEqual((blocked[-1]["decision"], blocked[-1]["reason"]),
                         ("block", "rule-x: no"))

    def test_a_script_the_shim_cannot_run_logs_an_error(self):
        with mock.patch("sys.stdin", StringIO("{}")), \
                mock.patch("sys.stdout", StringIO()), \
                mock.patch("sys.stderr", StringIO()), \
                mock.patch.object(shim_runtime.subprocess, "run",
                                  side_effect=OSError("nope")):
            rc = shim_runtime.main(["--contract", "{}", "--", "x.py"])
        self.assertEqual(rc, 0)
        (row,) = events.read_log(self.home)
        self.assertEqual(row["decision"], "error")

    def test_the_real_shim_file_logs_from_a_subprocess(self):
        script = self.home / "no-sleep-poll.py"
        script.write_text("import sys\nsys.stderr.write('no sleep\\n')\n"
                          "sys.exit(2)\n")
        proc = subprocess.run(
            [sys.executable, str(shim_runtime.__file__), "--contract",
             '{"harness": "claude"}', "--", str(script)],
            input=json.dumps({"hook_event_name": "PreToolUse",
                              "tool_name": "Bash", "session_id": "q"}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(proc.returncode, 2)
        (row,) = events.read_log(self.home)
        self.assertEqual((row["hook"], row["event"], row["decision"]),
                         ("no-sleep-poll", "PreToolUse:Bash", "block"))


# --- transcripts ------------------------------------------------------------


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rec(epoch, kind, **fields) -> str:
    base = {"type": kind, "timestamp": _stamp(epoch), "sessionId": "sess-1",
            "cwd": "/w/app", "gitBranch": "main", "uuid": "u"}
    base.update(fields)
    return json.dumps(base)


def _transcript(now: float) -> list[str]:
    cmd = "python3 /Users/x/.claude/hooks/_generated/no-sleep-poll.py"
    pg = "python3 /Users/x/.claude/hooks/prose-guard.py"
    t = now - 3 * HOUR
    return [
        # A plain allow with a duration.
        _rec(t, "attachment", attachment={
            "type": "hook_success", "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse", "toolUseID": "tu1", "command": cmd,
            "exitCode": 0, "durationMs": 31, "stderr": "", "stdout": ""}),
        # A block: the attachment-less tool result.
        _rec(t + 10, "user", message={"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tu2", "is_error": True,
            "content": f"PreToolUse:Bash hook error: [{cmd}]: Do not poll "
                       "with sleep\nuse a background command"}]}),
        # The same block also leaves an error attachment: counted once.
        _rec(t + 10, "attachment", attachment={
            "type": "hook_non_blocking_error", "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse", "toolUseID": "tu2", "command": cmd,
            "exitCode": 2, "durationMs": 44, "stderr": "Do not poll"}),
        # A non-blocking error and a cancel.
        _rec(t + 20, "attachment", attachment={
            "type": "hook_non_blocking_error", "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse", "toolUseID": "tu3", "command": cmd,
            "exitCode": 1, "durationMs": 5, "stderr": "Traceback\nboom"}),
        _rec(t + 30, "attachment", attachment={
            "type": "hook_cancelled", "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse", "toolUseID": "tu4", "command": cmd,
            "durationMs": 9}),
        # Stop: the summary and a feedback block for prose-guard.
        _rec(t + 100, "system", subtype="stop_hook_summary", hookInfos=[
            {"command": pg, "durationMs": 40}], hookErrors=[],
            preventedContinuation=False),
        _rec(t + 101, "user", isMeta=True, message={
            "role": "user", "content":
                f"Stop hook feedback:\n[{pg}]: done-followups: no Follow-ups"
                "\nsecond line"}),
        # prose-guard allowed after the rewrite.
        _rec(t + 160, "system", subtype="stop_hook_summary", hookInfos=[
            {"command": pg, "durationMs": 38}], hookErrors=[]),
        # Noise that mentions hooks but is not an event.
        _rec(t + 170, "user", message={"role": "user",
                                       "content": "let's talk about hooks"}),
        "not json hook",
    ]


class TranscriptParseTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.rows = transcripts.parse_lines(_transcript(self.now))

    def _by(self, hook, decision):
        return [e for e in self.rows
                if e["hook"] == hook and e["decision"] == decision]

    def test_command_hook_names(self):
        self.assertEqual(transcripts.command_hook(
            "python3 ~/.claude/hooks/_generated/no-sleep-poll.py --x"),
            "no-sleep-poll")
        self.assertEqual(transcripts.command_hook(
            "$HOME/.claude/hooks/pr-behind-main.sh"), "pr-behind-main")
        self.assertEqual(transcripts.command_hook("node"), "node")
        self.assertEqual(transcripts.command_hook(""), "")

    def test_each_run_is_one_event_of_the_right_kind(self):
        counts = {}
        for e in self.rows:
            counts[(e["hook"], e["decision"])] = \
                counts.get((e["hook"], e["decision"]), 0) + 1
        self.assertEqual(counts, {
            ("no-sleep-poll", "allow"): 1,
            ("no-sleep-poll", "block"): 1,
            ("no-sleep-poll", "error"): 2,
            ("prose-guard", "block"): 1,
            ("prose-guard", "allow"): 1,
        })

    def test_block_takes_its_reason_line_and_the_attachments_duration(self):
        (block,) = self._by("no-sleep-poll", "block")
        self.assertEqual(block["reason"], "Do not poll with sleep")
        self.assertEqual(block["event"], "PreToolUse:Bash")
        self.assertEqual(block["duration_ms"], 44)
        self.assertEqual(block["session_id"], "sess-1")
        self.assertEqual(block["cwd"], "/w/app")
        self.assertEqual(block["git_branch"], "main")
        self.assertEqual(block["harness"], "claude")
        self.assertEqual(block["source"], "transcript")
        self.assertNotIn("_kind", block)

    def test_error_and_cancel_reasons(self):
        reasons = sorted(e["reason"]
                         for e in self._by("no-sleep-poll", "error"))
        self.assertEqual(reasons, ["Traceback", "cancelled"])

    def test_stop_feedback_absorbs_its_summary(self):
        (block,) = self._by("prose-guard", "block")
        self.assertEqual(block["reason"], "done-followups: no Follow-ups")
        self.assertEqual(block["duration_ms"], 40)
        self.assertEqual(block["event"], "Stop")
        (allow,) = self._by("prose-guard", "allow")
        self.assertEqual(allow["duration_ms"], 38)

    def test_stop_warn_from_hook_errors_and_other_event_feedback(self):
        pg = "python3 /h/prose-guard.py"
        rows = transcripts.parse_lines([
            _rec(self.now, "system", subtype="stop_hook_summary",
                 hookInfos=[{"command": pg, "durationMs": 3}],
                 hookErrors=[f"[{pg}]: crashed\ntrace"]),
            _rec(self.now + 1, "user", isMeta=True, message={
                "role": "user", "content": [{
                    "type": "text",
                    "text": f"SubagentStop hook feedback:\n[{pg}]: nope"}]}),
        ])
        decisions = sorted((e["event"], e["decision"], e["reason"])
                           for e in rows)
        self.assertEqual(decisions, [("Stop", "error", "crashed"),
                                     ("SubagentStop", "block", "nope")])

    def test_blocking_error_attachment_and_success_with_stderr(self):
        cmd = "python3 /h/x.py"
        rows = transcripts.parse_lines([
            _rec(self.now, "attachment", attachment={
                "type": "hook_blocking_error", "hookName": "PreToolUse:Edit",
                "toolUseID": "a", "command": cmd, "stderr": "no edits"}),
            _rec(self.now + 1, "attachment", attachment={
                "type": "hook_success", "hookName": "SessionStart:startup",
                "toolUseID": "b", "command": cmd, "exitCode": 0,
                "stderr": "nudge\nmore"}),
            _rec(self.now + 2, "attachment", attachment={
                "type": "hook_additional_context", "command": cmd}),
        ])
        self.assertEqual(
            [(e["event"], e["decision"], e["reason"]) for e in rows],
            [("PreToolUse:Edit", "block", "no edits"),
             ("SessionStart:startup", "warn", "nudge")])

    def test_bad_input_is_skipped(self):
        self.assertEqual(transcripts.parse_lines([
            b"", b"hook", b'{"hook": 1}', b'[1, "hook"]',
            _rec(self.now, "attachment", attachment="hook").encode(),
            json.dumps({"type": "attachment", "attachment": {
                "type": "hook_success", "command": "x.py",
                "hookName": "Stop"}}).encode(),  # no timestamp
        ]), [])
        self.assertEqual(transcripts.parse_file("/no/such/file.jsonl"), [])


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.now = time.time()
        project = self.home / ".claude" / "projects" / "-w-app"
        project.mkdir(parents=True)
        self.path = project / "sess-1.jsonl"
        self.path.write_text("\n".join(_transcript(self.now)) + "\n")
        old = project / "old.jsonl"
        old.write_text("\n".join(_transcript(self.now - 30 * DAY)) + "\n")
        stale = self.now - 20 * DAY
        os.utime(old, (stale, stale))

    def test_reads_claude_transcripts_through_the_session_enumeration(self):
        found = transcripts.claude_transcripts(self.home)
        self.assertEqual(sorted(Path(p).name for p in found),
                         ["old.jsonl", "sess-1.jsonl"])
        recent = transcripts.claude_transcripts(self.home,
                                                self.now - 7 * DAY)
        self.assertEqual([Path(p).name for p in recent], ["sess-1.jsonl"])

    def test_backfill_honours_since_and_fills_repo(self):
        repo = self.home / "w" / "app"
        (repo / ".git").mkdir(parents=True)
        # Point the fixture's cwd at a real repo so repo resolution runs.
        text = self.path.read_text().replace("/w/app", str(repo))
        self.path.write_text(text)
        rows = transcripts.backfill(self.home, self.now - 7 * DAY)
        self.assertEqual(len(rows), 6)
        self.assertEqual({e["repo"] for e in rows}, {str(repo)})

    def test_the_first_enumeration_is_used_not_a_glob_per_file(self):
        from swe.sessions import search_writer
        with mock.patch.object(search_writer, "sources",
                               wraps=search_writer.sources) as spy:
            transcripts.backfill(self.home, self.now - 7 * DAY)
        self.assertEqual(spy.call_count, 1)


class MergeTest(unittest.TestCase):
    def _ev(self, ts, hook="prose-guard", event="Stop", session="s1",
            decision="block", source="helper", **extra):
        row = {"ts": ts, "hook": hook, "harness": "claude", "event": event,
               "session_id": session, "cwd": None, "repo": None,
               "git_branch": None, "decision": decision, "reason": "r",
               "duration_ms": None, "exit_code": None,
               "stop_hook_active": None, "source": source}
        row.update(extra)
        return row

    def test_same_run_in_both_sources_counts_once(self):
        helper = [self._ev(1000.0)]
        transcript = [self._ev(1002.5, source="transcript")]
        merged, dropped = events.merge_events(helper, transcript)
        self.assertEqual((len(merged), dropped), (1, 1))
        self.assertEqual(merged[0]["source"], "helper")

    def test_outside_the_window_or_another_session_is_a_new_run(self):
        helper = [self._ev(1000.0)]
        transcript = [
            self._ev(1000.0 + events.DEDUPE_WINDOW + 1, source="transcript"),
            self._ev(1000.0, session="other", source="transcript"),
            self._ev(1000.0, hook="other-hook", source="transcript"),
        ]
        merged, dropped = events.merge_events(helper, transcript)
        self.assertEqual((len(merged), dropped), (4, 0))

    def test_matching_is_one_to_one(self):
        helper = [self._ev(1000.0)]
        transcript = [self._ev(1000.5, source="transcript"),
                      self._ev(1001.0, source="transcript")]
        merged, dropped = events.merge_events(helper, transcript)
        self.assertEqual((len(merged), dropped), (2, 1))

    def test_tool_suffix_does_not_defeat_the_match(self):
        helper = [self._ev(1000.0, event="PreToolUse")]
        transcript = [self._ev(1001.0, event="PreToolUse:Bash",
                               source="transcript")]
        _, dropped = events.merge_events(helper, transcript)
        self.assertEqual(dropped, 1)

    def test_recovered_is_inferred_after_a_block_only_for_stop(self):
        rows = [
            self._ev(1.0, decision="block"),
            self._ev(2.0, decision="allow"),
            self._ev(3.0, decision="allow"),
            self._ev(4.0, decision="allow", event="PreToolUse:Bash"),
            self._ev(5.0, decision="block", stop_hook_active=False),
            self._ev(6.0, decision="allow", stop_hook_active=False),
        ]
        events.mark_recovered(rows)
        self.assertEqual([r["stop_hook_active"] for r in rows],
                         [False, True, False, None, False, False])


# --- the commands -----------------------------------------------------------


class StatsCommandTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.now = time.time()
        _write_log(self.home, _fixture_rows(self.now))

    def test_json_counts_for_the_last_seven_days(self):
        code, out, _ = _run(self.home, "stats", "--no-backfill", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 1)
        data = doc["data"]
        self.assertEqual(data["window"], "7d")
        self.assertEqual(data["sources"], {
            "helper": 8, "transcript": 0, "duplicates": 0,
            "backfill": False})
        rows = {(r["hook"], r["harness"]): r for r in data["hooks"]}
        pg = rows[("prose-guard", "claude")]
        self.assertEqual((pg["fires"], pg["blocks"], pg["allows"],
                          pg["recovered"]), (4, 2, 2, 1))
        self.assertEqual(pg["block_rate"], 0.5)
        self.assertEqual(pg["top_reasons"], [{
            "decision": "block", "count": 2,
            "reason": "done-followups: missing Follow-ups"}])
        nsp = rows[("no-sleep-poll", "claude")]
        self.assertEqual((nsp["fires"], nsp["blocks"], nsp["warns"]),
                         (3, 1, 1))
        self.assertEqual(nsp["block_rate"], 0.3333)
        cursor = rows[("prose-guard", "cursor")]
        self.assertEqual((cursor["fires"], cursor["errors"]), (1, 1))
        self.assertEqual(data["totals"]["fires"], 8)
        self.assertEqual(data["totals"]["blocks"], 3)
        self.assertEqual(data["totals"]["hooks"], 3)
        # The nine-day-old block is out of the window.
        self.assertNotIn("old", json.dumps(data))
        # Most blocks first.
        self.assertEqual(data["hooks"][0]["hook"], "prose-guard")

    def test_json_shape_is_pinned(self):
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json")
        data = json.loads(out)["data"]
        self.assertEqual(sorted(data), sorted([
            "window", "since", "until", "filters", "sources", "totals",
            "hooks"]))
        self.assertEqual(sorted(data["filters"]),
                         ["harness", "hook", "repo"])
        self.assertEqual(sorted(data["totals"]), sorted([
            "fires", "blocks", "warns", "errors", "allows", "recovered",
            "block_rate", "last_fired", "last_block", "avg_duration_ms",
            "hooks"]))
        self.assertEqual(sorted(data["hooks"][0]), sorted([
            "hook", "harness", "fires", "blocks", "warns", "errors",
            "allows", "recovered", "block_rate", "last_fired", "last_block",
            "avg_duration_ms", "top_reasons"]))
        self.assertAlmostEqual(data["until"] - data["since"], 7 * DAY, 2)

    def test_table_output(self):
        code, out, _ = _run(self.home, "stats", "--no-backfill")
        self.assertEqual(code, 0)
        self.assertIn("Hook stats", out)
        self.assertIn("last 7d", out)
        for header in ("HOOK", "FIRES", "BLOCKS", "RATE"):
            self.assertIn(header, out)
        row = next(x for x in out.splitlines()
                   if x.split()[:2] == ["prose-guard", "claude"])
        self.assertEqual(row.split()[2:5], ["4", "2", "50%"])
        self.assertIn("Top reasons", out)
        self.assertIn("done-followups: missing Follow-ups", out)
        self.assertIn("8 runs", out)

    def test_filters(self):
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--hook=no-sleep-poll.py")
        hooks = json.loads(out)["data"]["hooks"]
        self.assertEqual([h["hook"] for h in hooks], ["no-sleep-poll"])
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--harness=cursor")
        self.assertEqual(
            [(h["hook"], h["harness"])
             for h in json.loads(out)["data"]["hooks"]],
            [("prose-guard", "cursor")])
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--since=4h")
        self.assertEqual(json.loads(out)["data"]["totals"]["fires"], 2)
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--since=all")
        self.assertEqual(json.loads(out)["data"]["totals"]["fires"], 9)

    def test_repo_filter(self):
        other = [_line(self.now, HOUR, "prose-guard", "block",
                       cwd="/w/other", repo="/w/other")]
        _write_log(self.home, _fixture_rows(self.now) + other)
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--repo=/w/other")
        data = json.loads(out)["data"]
        self.assertEqual(data["totals"]["fires"], 1)
        # A folder inside the repo matches on the recorded cwd.
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json",
                         "--repo=/w/app/src")
        self.assertEqual(json.loads(out)["data"]["totals"]["fires"], 0)

    def test_empty_window_and_missing_log(self):
        home = temp_dir(self)
        code, out, _ = _run(home, "stats", "--no-backfill")
        self.assertEqual(code, 0)
        self.assertIn("No hook runs recorded", out)
        _, out, _ = _run(home, "stats", "--no-backfill", "--json")
        data = json.loads(out)["data"]
        self.assertEqual((data["hooks"], data["totals"]["fires"]), ([], 0))

    def test_bad_flags(self):
        for args in (["--since=soon"], ["--since"], ["--since=0d"],
                     ["extra"], ["--decision=block"], ["--bogus"]):
            code, out, err = _run(self.home, "stats", "--no-backfill",
                                  *args)
            self.assertEqual(code, 1, args)

    def test_json_errors_go_to_stderr(self):
        code, out, err = _run(self.home, "stats", "--since=soon", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--since=soon", err)

    def test_garbage_in_the_log_is_skipped(self):
        path = paths.hook_events_file_for(self.home)
        with open(path, "a") as handle:
            handle.write("not json\n{\"ts\": \"x\"}\n[]\n{\"ts\": 1}\n"
                         '{"ts": 1, "hook": "h", "decision": "huh"}\n'
                         '{"ts": 1, "hook": "h", "decision": "allow"')
        _, out, _ = _run(self.home, "stats", "--no-backfill", "--json")
        self.assertEqual(json.loads(out)["data"]["totals"]["fires"], 8)

    def test_helper_and_transcripts_merge_without_double_counting(self):
        project = self.home / ".claude" / "projects" / "-w-app"
        project.mkdir(parents=True)
        pg = "python3 /Users/x/.claude/hooks/prose-guard.py"
        # The same block the log holds one hour ago, plus a run only the
        # transcript knows about.
        first = self.now - 1 * HOUR
        (project / "sess-1.jsonl").write_text("\n".join([
            _rec(first + 1, "user", isMeta=True, sessionId="s1", message={
                "role": "user",
                "content": f"Stop hook feedback:\n[{pg}]: dup"}),
            _rec(self.now - 4 * DAY, "user", isMeta=True, sessionId="s1",
                 message={
                "role": "user",
                "content": f"Stop hook feedback:\n[{pg}]: only here"}),
        ]) + "\n")
        _, out, _ = _run(self.home, "stats", "--json", "--hook=prose-guard",
                         "--harness=claude")
        data = json.loads(out)["data"]
        self.assertEqual(data["sources"]["transcript"], 1)
        self.assertEqual(data["sources"]["duplicates"], 1)
        (row,) = data["hooks"]
        self.assertEqual((row["fires"], row["blocks"]), (5, 3))
        reasons = {r["reason"] for r in row["top_reasons"]}
        self.assertIn("only here", reasons)
        self.assertNotIn("dup", reasons)


class LogCommandTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.now = time.time()
        _write_log(self.home, _fixture_rows(self.now))

    def test_json_newest_first(self):
        code, out, _ = _run(self.home, "log", "prose-guard",
                            "--no-backfill", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["hook"], "prose-guard")
        self.assertEqual(data["matched"], 5)
        stamps = [e["ts"] for e in data["events"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(sorted(data["events"][0]), sorted([
            "ts", "hook", "harness", "event", "session_id", "cwd", "repo",
            "git_branch", "decision", "reason", "duration_ms", "exit_code",
            "stop_hook_active", "source"]))

    def test_decision_and_limit(self):
        _, out, _ = _run(self.home, "log", "prose-guard.py", "--json",
                         "--no-backfill", "--decision=block", "--limit=1")
        data = json.loads(out)["data"]
        self.assertEqual((data["matched"], len(data["events"])), (2, 1))
        self.assertEqual(data["events"][0]["decision"], "block")

    def test_table(self):
        code, out, _ = _run(self.home, "log", "no-sleep-poll",
                            "--no-backfill")
        self.assertEqual(code, 0)
        self.assertIn("no-sleep-poll", out)
        self.assertIn("3 of 3 runs", out)
        self.assertIn("Do not poll with sleep", out)
        self.assertIn("PreToolUse:Bash", out)

    def test_nothing_and_bad_args(self):
        code, out, _ = _run(self.home, "log", "ghost", "--no-backfill")
        self.assertEqual(code, 0)
        self.assertIn("No runs of that hook", out)
        for args in ([], ["a", "b"], ["x", "--decision=nope"],
                     ["x", "--limit=0"], ["x", "--limit=many"],
                     ["x", "--hook=y"]):
            code, _, _ = _run(self.home, "log", *args)
            self.assertEqual(code, 1, args)


class PrefixedNameTest(unittest.TestCase):
    """A hook another installer declared is named ``folder/stem`` in the
    stats, and ``--hook=`` / ``log <name>`` take that name back."""

    def setUp(self):
        self.home = temp_dir(self)
        self.now = time.time()
        # A bare ``on-stop`` in the log, and a plugin's ``on-stop`` that only
        # a transcript knows, under its folder prefix.
        _write_log(self.home, [_line(self.now, HOUR, "on-stop", "allow")])
        project = self.home / ".claude" / "projects" / "-w-app"
        project.mkdir(parents=True)
        plugin = "${CLAUDE_PLUGIN_ROOT}/hooks/on-stop.sh"
        (project / "s.jsonl").write_text("\n".join([
            _rec(self.now - 2 * HOUR, "user", isMeta=True, sessionId="p1",
                 message={"role": "user", "content":
                          f"Stop hook feedback:\n[{plugin}]: plugin said no"}),
            _rec(self.now - 3 * HOUR, "user", isMeta=True, sessionId="p2",
                 message={"role": "user", "content":
                          f"Stop hook feedback:\n[{plugin}]: plugin said no"}),
        ]) + "\n")

    def _log(self, name):
        _, out, _ = _run(self.home, "log", name, "--json")
        return json.loads(out)["data"]

    def test_the_stats_name_round_trips_through_log(self):
        _, out, _ = _run(self.home, "stats", "--json")
        names = {r["hook"] for r in json.loads(out)["data"]["hooks"]}
        self.assertEqual(names, {"on-stop", "claude-plugin/on-stop"})
        data = self._log("claude-plugin/on-stop")
        self.assertEqual(data["hook"], "claude-plugin/on-stop")
        self.assertEqual(data["matched"], 2)
        self.assertEqual({e["hook"] for e in data["events"]},
                         {"claude-plugin/on-stop"})

    def test_a_bare_name_does_not_pick_up_the_prefixed_hook(self):
        data = self._log("on-stop")
        self.assertEqual(data["matched"], 1)
        self.assertEqual({e["hook"] for e in data["events"]}, {"on-stop"})

    def test_a_file_name_still_finds_its_hook(self):
        self.assertEqual(self._log("on-stop.sh")["matched"], 1)
        self.assertEqual(self._log("claude-plugin/on-stop.sh")["matched"], 2)
        self.assertEqual(self._log("/x/hooks/on-stop.py")["matched"], 1)

    def test_stats_hook_filter_takes_the_exact_name(self):
        _, out, _ = _run(self.home, "stats", "--hook=claude-plugin/on-stop",
                         "--json")
        rows = json.loads(out)["data"]["hooks"]
        self.assertEqual([(r["hook"], r["fires"]) for r in rows],
                         [("claude-plugin/on-stop", 2)])
        _, out, _ = _run(self.home, "stats", "--hook=on-stop.sh", "--json")
        rows = json.loads(out)["data"]["hooks"]
        self.assertEqual([(r["hook"], r["fires"]) for r in rows],
                         [("on-stop", 1)])


class SumHarnessesTest(unittest.TestCase):
    """``stats --sum-harnesses`` folds harnesses together before the
    five-reason cap, so a reason split across harnesses totals exactly."""

    def setUp(self):
        self.home = temp_dir(self)
        self.now = time.time()
        rows = []
        for n in range(5):
            rows += [_line(self.now, HOUR + i, "pg", "block",
                           reason=f"big-{n}", exit_code=2, session_id=f"a{n}{i}")
                     for i in range(10)]
        # Nine on Claude falls out of Claude's top five; nine more on
        # Cursor brings the total to eighteen, the biggest of all.
        rows += [_line(self.now, 2 * HOUR + i, "pg", "block", reason="split",
                       exit_code=2, session_id=f"c{i}") for i in range(9)]
        rows += [_line(self.now, 3 * HOUR + i, "pg", "block", reason="split",
                       exit_code=2, harness="cursor", session_id=f"d{i}")
                 for i in range(9)]
        _write_log(self.home, rows)

    def _stats(self, *flags):
        code, out, err = _run(self.home, "stats", "--no-backfill", "--json",
                              *flags)
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def test_the_default_still_has_one_row_per_harness(self):
        rows = self._stats()["hooks"]
        self.assertEqual({r["harness"] for r in rows}, {"claude", "cursor"})

    def test_summed_rows_total_reasons_before_the_cap(self):
        data = self._stats("--sum-harnesses")
        (row,) = data["hooks"]
        self.assertEqual((row["hook"], row["harness"]), ("pg", ""))
        self.assertEqual((row["fires"], row["blocks"]), (68, 68))
        top = row["top_reasons"]
        self.assertEqual(len(top), 5)
        self.assertEqual((top[0]["reason"], top[0]["count"]), ("split", 18))
        self.assertEqual(data["totals"]["fires"], 68)

    def test_log_does_not_take_the_flag(self):
        code, _, _ = _run(self.home, "log", "pg", "--sum-harnesses")
        self.assertEqual(code, 1)


class HelperUnitTest(unittest.TestCase):
    def test_hook_stem_and_since(self):
        self.assertEqual(events.hook_stem("prose-guard.py"), "prose-guard")
        self.assertEqual(events.hook_stem("/a/b/command_guard.sh"),
                         "command_guard")
        self.assertEqual(events.hook_stem("noext"), "noext")
        self.assertEqual(events.hook_stem(".py"), ".py")
        self.assertEqual(events.parse_since("2w", 1_000_000),
                         1_000_000 - 2 * 604800)
        self.assertEqual(events.parse_since("90m", 10_000), 10_000 - 5400)
        self.assertIsNone(events.parse_since("ALL", 5))
        for bad in ("", "7", "d", "-1d", "1y"):
            with self.assertRaises(ValueError):
                events.parse_since(bad, 5)

    def test_resolve_harness_aliases(self):
        registry = {"claude": {"aliases": ["cc"]}}
        self.assertEqual(
            stats_commands._resolve_harness("cc", registry, set()), "claude")
        self.assertEqual(
            stats_commands._resolve_harness("zzz", registry, set()), "zzz")


class RuntimeCopyTest(unittest.TestCase):
    def test_the_shim_stays_stdlib_only(self):
        source = Path(shim_runtime.__file__).read_text()
        imports = set(re.findall(r"^(?:import|from) (\w+)", source,
                                 re.MULTILINE))
        self.assertLessEqual(
            imports,
            {"__future__", "json", "os", "subprocess", "sys", "time"})


# --- shapes seen in real transcripts (paths and ids anonymised) -------------


class RealShapeTest(unittest.TestCase):
    NSP = 'python3 "/h/.claude/hooks/no-sleep-poll.py"'
    PG = 'python3 "/h/.claude/hooks/prose-guard.py"'
    GUARD = ('f="$HOME/.swe/hooks/command-guard/command_guard.py"; '
             '[ -f "$f" ] || exit 0; exec python3 "$f"')

    def setUp(self):
        self.now = time.time() - 600

    def test_names_from_real_command_strings(self):
        names = {
            self.GUARD: "command_guard",
            self.NSP: "no-sleep-poll",
            'python3 "/h/.swe/hooks/claude/_generated/prose-guard.py"':
                "prose-guard",
            '"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"':
                "claude-plugin/session-start",
            "${CLAUDE_PLUGIN_ROOT}/scripts/on-stop.sh":
                "claude-plugin/on-stop",
            'PATH="/etc/profiles/per-user/$USER/bin:$PATH" node '
            '"/h/.pixel-agents/hooks/claude-hook.js"':
                ".pixel-agents/claude-hook",
            '[ -n "$SUPERSET_HOME_DIR" ] && [ -x '
            '"$SUPERSET_HOME_DIR/hooks/notify.sh" ] && '
            'SUPERSET_AGENT_ID=claude "$SUPERSET_HOME_DIR/hooks/notify.sh" '
            '|| true': "superset/notify",
            "callback": "callback",
            "A lot of things have happened so far. I pushed many PRs and "
            "another agent is resolving them. Please update yourself": "prompt",
            "": "",
        }
        for command, name in names.items():
            self.assertEqual(transcripts.command_hook(command), name,
                             command[:40])

    def test_the_log_and_the_transcript_name_a_shared_hook_alike(self):
        stub = '/h/.swe/hooks/claude/_generated/prose-guard.py'
        self.assertEqual(transcripts.command_hook(f'python3 "{stub}"'),
                         events.hook_stem(stub))

    def test_exit_2_block_is_one_block_not_a_warning(self):
        # Claude records the exit 2 as a hook_success carrying the stderr,
        # then the tool result. Together they are one block.
        rows = transcripts.parse_lines([
            _rec(self.now, "attachment", attachment={
                "type": "hook_success", "hookName": "PreToolUse:Bash",
                "hookEvent": "PreToolUse", "toolUseID": "toolu_1",
                "content": "", "stdout": "", "exitCode": 0,
                "stderr": "Blocked: synchronous polling. Arm the monitor",
                "command": self.NSP, "durationMs": 22}),
            _rec(self.now + 0.1, "user", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "is_error": True,
                 "content": f"PreToolUse:Bash hook error: [{self.NSP}]: "
                            "Blocked: synchronous polling. Arm the monitor"
                 }]}),
        ])
        self.assertEqual([(e["hook"], e["decision"], e["duration_ms"])
                          for e in rows], [("no-sleep-poll", "block", 22)])

    def test_hook_success_with_stderr_and_no_block_is_a_warning(self):
        rows = transcripts.parse_lines([_rec(
            self.now, "attachment", attachment={
                "type": "hook_success", "hookName": "PreToolUse:Bash",
                "toolUseID": "toolu_2", "stdout": "", "exitCode": 0,
                "stderr": "Blocked: synchronous polling",
                "command": self.NSP, "durationMs": 9})])
        self.assertEqual([e["decision"] for e in rows], ["warn"])

    def test_block_with_a_bracketed_shell_command(self):
        rows = transcripts.parse_lines([_rec(
            self.now, "user", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "is_error": True,
                 "content": f"PreToolUse:Bash hook error: [{self.GUARD}]: "
                            "command-guard blocked this command."}]})])
        self.assertEqual([(e["hook"], e["decision"], e["reason"])
                          for e in rows],
                         [("command_guard", "block",
                           "command-guard blocked this command.")])

    def test_a_stop_run_in_three_records_is_one_block(self):
        rows = transcripts.parse_lines([
            _rec(self.now, "attachment", attachment={
                "type": "hook_success", "hookName": "Stop",
                "hookEvent": "Stop", "toolUseID": "u", "exitCode": 0,
                "stderr": "Your reply breaks the style rules.",
                "stdout": "", "command": self.PG, "durationMs": 61}),
            _rec(self.now + 0.05, "system", subtype="stop_hook_summary",
                 hookCount=2, hookInfos=[
                     {"command": self.PG, "durationMs": 61},
                     {"command": "${CLAUDE_PLUGIN_ROOT}/scripts/on-stop.sh",
                      "durationMs": 12}], hookErrors=[],
                 preventedContinuation=False, stopReason=""),
            _rec(self.now + 1, "user", isMeta=True, message={
                "role": "user", "content":
                    f"Stop hook feedback:\n[{self.PG}]: Your reply breaks "
                    "the style rules."}),
        ])
        got = sorted((e["hook"], e["decision"]) for e in rows)
        self.assertEqual(got, [("claude-plugin/on-stop", "allow"),
                               ("prose-guard", "block")])

    def test_stop_attachment_and_summary_entry_are_one_run(self):
        rows = transcripts.parse_lines([
            _rec(self.now, "attachment", attachment={
                "type": "hook_non_blocking_error", "hookName": "Stop",
                "hookEvent": "Stop", "toolUseID": "u", "exitCode": 1,
                "stderr": "Failed with non-blocking status code: boom\ntrace",
                "stdout": "", "durationMs": 40,
                "command": 'node "/h/.pixel-agents/hooks/claude-hook.js"'}),
            _rec(self.now + 0.01, "system", subtype="stop_hook_summary",
                 hookInfos=[{"command":
                             'node "/h/.pixel-agents/hooks/claude-hook.js"',
                             "durationMs": 40}],
                 hookErrors=["Failed with non-blocking status code: boom"]),
        ])
        self.assertEqual([(e["decision"], e["reason"]) for e in rows],
                         [("error", "Failed with non-blocking status code: "
                                    "boom")])

    def test_additional_context_is_not_a_run(self):
        self.assertEqual(transcripts.parse_lines([_rec(
            self.now, "attachment", attachment={
                "type": "hook_additional_context", "hookName": "SessionStart",
                "toolUseID": "SessionStart", "hookEvent": "SessionStart",
                "content": ["some context"]})]), [])

    def test_timed_out_hook(self):
        rows = transcripts.parse_lines([_rec(
            self.now, "attachment", attachment={
                "type": "hook_cancelled", "hookName": "SessionStart:compact",
                "toolUseID": "c", "hookEvent": "SessionStart",
                "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"',
                "durationMs": 8037, "timedOut": True, "timeoutMs": 8000})])
        self.assertEqual([(e["decision"], e["reason"]) for e in rows],
                         [("error", "timed out")])

    def test_a_line_that_only_talks_about_hooks_is_never_parsed(self):
        with mock.patch.object(transcripts.json, "loads",
                               side_effect=AssertionError) as loads:
            transcripts.parse_lines([
                b'{"type":"assistant","text":"the hook ran and it was fine"}'])
        loads.assert_not_called()

    def test_an_error_names_only_the_hook_it_names(self):
        rows = transcripts.parse_lines([_rec(
            self.now, "system", subtype="stop_hook_summary",
            hookInfos=[
                {"command": 'python3 "/h/hooks/guard.py"', "durationMs": 1},
                {"command": 'python3 "/h/hooks/prose-guard.py"',
                 "durationMs": 2}],
            hookErrors=['[python3 "/h/hooks/prose-guard.py"]: crashed'])])
        self.assertEqual(
            sorted((e["hook"], e["decision"]) for e in rows),
            [("guard", "allow"), ("prose-guard", "error")])
        # An error that names no command is not pinned on anyone.
        rows = transcripts.parse_lines([_rec(
            self.now, "system", subtype="stop_hook_summary",
            hookInfos=[{"command": 'python3 "/h/hooks/guard.py"',
                        "durationMs": 1}],
            hookErrors=["Failed with non-blocking status code: boom"])])
        self.assertEqual([e["decision"] for e in rows], ["allow"])


class ReviewFixesTest(unittest.TestCase):
    """The behaviours the PR review asked for."""

    def _ev(self, ts, **kw):
        return MergeTest._ev(self, ts, **kw)

    def test_the_window_matches_copies_across_the_whole_group(self):
        # Helper runs at 100 and 104, transcript copies at 104 and 108.
        helper = [self._ev(100.0), self._ev(104.0)]
        transcript = [self._ev(104.0, source="transcript"),
                      self._ev(108.0, source="transcript")]
        merged, dropped = events.merge_events(helper, transcript)
        self.assertEqual((len(merged), dropped), (2, 2))

    def test_a_retry_inside_the_window_keeps_its_block_from_before_it(self):
        home = temp_dir(self)
        now = time.time()
        pg = "python3 /h/prose-guard.py"
        project = home / ".claude" / "projects" / "-p"
        project.mkdir(parents=True)
        since = now - HOUR
        (project / "s.jsonl").write_text("\n".join([
            _rec(since - 30, "user", isMeta=True, sessionId="x", message={
                "role": "user", "content":
                    f"Stop hook feedback:\n[{pg}]: no"}),
            _rec(since + 30, "system", subtype="stop_hook_summary",
                 sessionId="x",
                 hookInfos=[{"command": pg, "durationMs": 3}]),
        ]) + "\n")
        recent = stats_commands.collect(home, since, True)["events"]
        self.assertEqual([(e["decision"], e["recovered"]) for e in recent],
                         [("allow", True)])
        everything = stats_commands.collect(home, None, True)["events"]
        self.assertEqual([e["recovered"] for e in everything],
                         [False, True])

    def test_recovery_needs_a_block_from_the_same_hook(self):
        rows = [self._ev(1.0, hook="blocker", decision="block"),
                self._ev(2.0, hook="always-allows", decision="allow",
                         stop_hook_active=True),
                self._ev(2.1, hook="blocker", decision="allow",
                         stop_hook_active=True)]
        events.mark_recovered(rows)
        self.assertEqual({r["hook"]: r["recovered"] for r in rows
                          if r["decision"] == "allow"},
                         {"always-allows": False, "blocker": True})

    def test_repo_filter_leaves_out_a_nested_repository(self):
        rows = [
            {**self._ev(1.0), "repo": "/work/app", "cwd": "/work/app/src"},
            {**self._ev(2.0), "repo": "/work/app/vendor/lib",
             "cwd": "/work/app/vendor/lib"},
            {**self._ev(3.0), "repo": None, "cwd": "/work/app/gone"},
        ]
        kept = events.filter_events(rows, repo="/work/app")
        self.assertEqual([r["ts"] for r in kept], [1.0, 3.0])

    def test_subagent_transcripts_are_read(self):
        home = temp_dir(self)
        now = time.time()
        pg = "python3 /h/prose-guard.py"
        session = home / ".claude" / "projects" / "-p" / "sess-9"
        (session / "subagents").mkdir(parents=True)
        (home / ".claude" / "projects" / "-p" / "sess-9.jsonl").write_text(
            _rec(now - 5, "user", message={"role": "user", "content": "hi"})
            + "\n")
        (session / "subagents" / "agent-1.jsonl").write_text(_rec(
            now - 4, "attachment", attachment={
                "type": "hook_success", "hookName": "SubagentStop",
                "hookEvent": "SubagentStop", "toolUseID": "z",
                "exitCode": 0, "stderr": "", "command": pg,
                "durationMs": 5}) + "\n")
        rows = transcripts.backfill(home, now - DAY)
        self.assertEqual([(e["hook"], e["event"]) for e in rows],
                         [("prose-guard", "SubagentStop")])

    def test_cold_scan_time_is_linear_in_command_length(self):
        # A long command once took milliseconds per call in the path regex.
        start = time.time()
        for i in range(200):
            transcripts.command_hook("x" * 4000 + f" {i} --flag=" + "y" * 500)
        self.assertLess(time.time() - start, 2.0)


if __name__ == "__main__":
    unittest.main()
