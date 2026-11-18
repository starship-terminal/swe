"""One conversation continued across two transcripts is listed once.

Claude Code can continue a session in a fresh transcript and writes
``{"type": "continued-in", "continuedInSessionId": ...}`` into the old file
when it happens. Both files then carry the same title, so listing both reads
as a duplicate row.

The marker alone does not end the old session, though: work often carries on
in it afterwards, and the two files are then genuinely separate sessions. It
is over only when no conversation follows the handoff.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.sessions.parsers import (
    _claude_handed_off,
    _claude_transcript_paths,
    parse_claude,
)

REAL_EXPANDUSER = os.path.expanduser
PROJECT = "-Users-dev-project"
CWD = "/Users/dev/project"


def _user(text):
    return {
        "type": "user",
        "cwd": CWD,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(text="ok"):
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


def _ai_title(text):
    return {"type": "ai-title", "aiTitle": text}


def _continued_in(session_id):
    return {"type": "continued-in", "continuedInSessionId": session_id}


class ClaudeHandoffTest(unittest.TestCase):
    def _run(self, transcripts):
        """``transcripts`` maps session id -> list of records."""
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project_dir = projects / PROJECT
            project_dir.mkdir(parents=True)
            # Claude Code's projects root holds more than project dirs, and a
            # project dir holds more than transcripts. Both are walked here,
            # so both decoys stay in every fixture.
            (projects / "not-a-project").mkdir()
            (project_dir / "notes.txt").write_text("ignore me")
            for session_id, records in transcripts.items():
                with open(project_dir / f"{session_id}.jsonl", "w") as f:
                    for rec in records:
                        f.write(json.dumps(rec) + "\n")

            def fake_expanduser(path):
                if path.startswith("~/.claude/projects"):
                    return str(projects) + "/"
                # Account-home scans must not read the real ~/.swe, and a
                # mirror walk must not read the real transcripts folder.
                if path.startswith("~/.swe") or path.startswith("~"):
                    return str(Path(tmp) / ".swe-empty") + path[len("~"):]
                return REAL_EXPANDUSER(path)

            with patch(
                "swe.sessions.parsers.os.path.expanduser", side_effect=fake_expanduser
            ):
                sessions = parse_claude()
        return {s.session_id: s for s in sessions}

    def test_both_transcripts_listed_without_a_handoff(self):
        got = self._run(
            {
                "aaa": [_user("Fix the login bug"), _ai_title("Login fix")],
                "bbb": [_user("Ship the parser"), _ai_title("Parser work")],
            }
        )
        self.assertEqual({"aaa", "bbb"}, set(got))

    def test_predecessor_dropped_when_nothing_follows_the_handoff(self):
        got = self._run(
            {
                "old": [_user("Fix the login bug"), _ai_title("Login fix"),
                        _continued_in("new")],
                "new": [_user("Fix the login bug"), _ai_title("Login fix")],
            }
        )
        self.assertEqual({"new"}, set(got))

    def test_predecessor_kept_when_the_conversation_carried_on(self):
        # The fork happened, then the user kept working in the original.
        # Two real sessions, however alike their titles look.
        got = self._run(
            {
                "old": [
                    _user("Fix the login bug"),
                    _ai_title("Login fix"),
                    _continued_in("new"),
                    _user("actually also fix signup"),
                    _assistant(),
                ],
                "new": [_user("Fix the login bug"), _ai_title("Login fix")],
            }
        )
        self.assertEqual({"old", "new"}, set(got))

    def test_bookkeeping_records_after_the_handoff_do_not_count(self):
        # Only conversation keeps a session alive; the harness writes plenty
        # of its own records on the way out.
        got = self._run(
            {
                "old": [
                    _user("Fix the login bug"),
                    _continued_in("new"),
                    {"type": "cost-state", "total": 1},
                    {"type": "file-history-snapshot", "files": []},
                ],
                "new": [_user("Fix the login bug")],
            }
        )
        self.assertEqual({"new"}, set(got))

    def test_predecessor_kept_when_the_successor_is_missing(self):
        # Dropping it would take the whole conversation off the listing.
        got = self._run({"old": [_user("Fix the login bug"), _continued_in("gone")]})
        self.assertEqual({"old"}, set(got))

    def test_a_chain_of_handoffs_leaves_only_the_last(self):
        got = self._run(
            {
                "first": [_user("Start"), _continued_in("second")],
                "second": [_user("Start"), _continued_in("third")],
                "third": [_user("Start")],
            }
        )
        self.assertEqual({"third"}, set(got))

    def test_malformed_marker_is_ignored(self):
        got = self._run(
            {
                "old": [_user("Fix the login bug"), {"type": "continued-in"}],
                "new": [_user("Fix the login bug")],
            }
        )
        self.assertEqual({"old", "new"}, set(got))

    def test_surviving_session_keeps_its_title_and_path(self):
        got = self._run(
            {
                "old": [_user("Fix the login bug"), _continued_in("new")],
                "new": [_user("Fix the login bug"), _ai_title("Login fix")],
            }
        )
        self.assertEqual("Login fix", got["new"].title)
        self.assertEqual("auto", got["new"].title_source)
        self.assertEqual(CWD, got["new"].path)


class ClaudeHandoffEdgeCaseTest(unittest.TestCase):
    """The tail read has to survive whatever is actually in these files."""

    def test_unreadable_project_root_yields_no_paths(self):
        self.assertEqual({}, _claude_transcript_paths("/nonexistent/projects"))

    def test_unreadable_transcript_is_not_treated_as_a_handoff(self):
        self.assertEqual("", _claude_handed_off("/nonexistent/session.jsonl"))

    def test_marker_at_the_end_of_a_file_past_the_tail_window(self):
        # The tail read starts mid-record on any file over 8KB, so the first
        # line of the chunk is a fragment and has to be dropped.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.jsonl"
            with open(path, "w") as f:
                f.write(json.dumps(_user("x" * 20000)) + "\n")
                f.write(json.dumps(_continued_in("next")) + "\n")
            self.assertEqual("next", _claude_handed_off(str(path)))

    def test_malformed_and_non_dict_records_in_the_tail_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            with open(path, "w") as f:
                f.write(json.dumps(_user("hello")) + "\n")
                f.write(json.dumps(_continued_in("next")) + "\n")
                f.write("not json at all\n")
                f.write("[1, 2, 3]\n")
                f.write("\n")
            self.assertEqual("next", _claude_handed_off(str(path)))

    def test_a_compressed_copy_still_names_its_successor(self):
        import shutil
        import subprocess

        if shutil.which("zstd") is None:
            self.skipTest("zstd is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            body = (json.dumps(_user("hello")) + "\n"
                    + json.dumps(_continued_in("next")) + "\n")
            path.write_bytes(subprocess.run(
                ["zstd", "-3", "-q", "-c"], input=body.encode(),
                capture_output=True, check=True).stdout)
            self.assertEqual("next", _claude_handed_off(str(path)))

    def test_a_tail_of_pure_bookkeeping_is_not_a_handoff(self):
        # Neither conversation nor a marker in the window: nothing to say.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            with open(path, "w") as f:
                f.write(json.dumps(_user("hello")) + "\n")
                for _ in range(200):
                    f.write(json.dumps({"type": "cost-state", "pad": "y" * 100}) + "\n")
            self.assertEqual("", _claude_handed_off(str(path)))


if __name__ == "__main__":
    unittest.main()
