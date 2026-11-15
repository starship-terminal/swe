"""Unit tests for newly added session parsers (copilot, continue, crush, grok)."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from tests._settled import settle_cache


class ParseCopilotTest(unittest.TestCase):
    def test_reads_sessions_and_title_from_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "session-store.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT,
                    summary TEXT,
                    updated_at TEXT,
                    created_at TEXT
                );
                CREATE TABLE turns (
                    session_id TEXT,
                    turn_index INTEGER,
                    user_message TEXT
                );
                CREATE TABLE checkpoints (
                    session_id TEXT,
                    checkpoint_number INTEGER,
                    title TEXT
                );
                INSERT INTO sessions VALUES
                    ('s1', '/tmp/proj', '', '2026-07-01T12:00:00Z', '2026-07-01T11:00:00Z');
                INSERT INTO turns VALUES ('s1', 0, 'fix the login bug');
                """
            )
            conn.commit()
            conn.close()

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(db) if p.endswith("session-store.db") else p,
            ):
                from swe.sessions.parsers import parse_copilot

                sessions = parse_copilot()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "copilot")
            self.assertEqual(sessions[0].path, "/tmp/proj")
            self.assertEqual(sessions[0].title, "fix the login bug")
            self.assertEqual(sessions[0].title_source, "")
            self.assertEqual(sessions[0].session_id, "s1")


_DEVIN_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    working_directory TEXT NOT NULL,
    backend_type TEXT NOT NULL,
    model TEXT NOT NULL,
    agent_mode TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL,
    title TEXT,
    main_chain_id INTEGER,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    is_shell INTEGER NOT NULL DEFAULT 0
);
"""


class ParseDevinTest(unittest.TestCase):
    """Devin CLI sessions.db: titles, provenance from prompt_history, hidden rows."""

    def _parse(self, sessions, prompts):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sessions.db"
            conn = sqlite3.connect(db)
            conn.executescript(_DEVIN_SCHEMA)
            conn.executemany(
                "INSERT INTO sessions VALUES (?, ?, 'windsurf', 'swe-2', 'normal', ?, ?, ?, NULL, ?)",
                sessions,
            )
            conn.executemany(
                "INSERT INTO prompt_history (content, timestamp, session_id, is_shell) VALUES (?, ?, ?, ?)",
                prompts,
            )
            conn.commit()
            conn.close()
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(db) if p.endswith("devin/cli/sessions.db") else p,
            ):
                from swe.sessions.parsers import parse_devin

                return {s.session_id: s for s in parse_devin()}

    def test_reads_sessions_with_seconds_timestamps_and_skips_hidden(self):
        found = self._parse(
            [
                ("shown", "/tmp/proj", 1789116383, 1789116437, "Create a connection string", 0),
                ("hidden", "/tmp/proj", 1789116383, 1789116437, "gone", 1),
            ],
            [],
        )
        self.assertEqual(set(found), {"shown"})
        s = found["shown"]
        self.assertEqual(s.tool_name, "devin")
        self.assertEqual(s.agent, "Devin")
        self.assertEqual(s.path, "/tmp/proj")
        self.assertEqual(s.title, "Create a connection string")
        self.assertEqual(s.timestamp, 1789116437000.0)

    def test_title_falls_back_to_first_prompt_of_the_session(self):
        found = self._parse(
            [("s1", "/tmp/proj", 1000, 1010, "", 0)],
            [
                ("/usage", 990, "s1", 0),            # typed before the session existed
                ("ls -la", 1000, "s1", 1),           # shell mode, not a prompt
                ("fix the login bug", 1000, "s1", 0),
                ("and the logout one", 1005, "s1", 0),
            ],
        )
        self.assertEqual(found["s1"].title, "fix the login bug")
        self.assertEqual(found["s1"].title_source, "")

    def test_prompts_are_read_in_one_query_for_every_session(self):
        import sqlite3 as real_sqlite3

        statements = []
        real_connect = real_sqlite3.connect

        def traced(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch("sqlite3.connect", side_effect=traced):
            found = self._parse(
                [("s1", "/tmp/a", 1000, 1010, "", 0), ("s2", "/tmp/b", 2000, 2010, "", 0),
                 ("s3", "/tmp/c", "bad", 3010, "", 0)],
                [("early", 990, "s1", 0), ("first one", 1000, "s1", 0),
                 ("second one", 2001, "s2", 0), ("third one", 5, "s3", 0)],
            )
        self.assertEqual({sid: s.title for sid, s in found.items()},
                         {"s1": "first one", "s2": "second one", "s3": "third one"})
        self.assertEqual(sum(s.startswith("SELECT") and "prompt_history" in s for s in statements), 1)

    def test_title_equal_to_first_prompt_has_no_provenance(self):
        found = self._parse(
            [("s1", "/tmp/proj", 1000, 1010, "/exi", 0)],
            [("/login-status", 995, "s1", 0), ("/exi", 1000, "s1", 0), ("/exit", 1003, "s1", 0)],
        )
        self.assertEqual(found["s1"].title, "/exi")
        self.assertEqual(found["s1"].title_source, "")

    def test_title_differing_from_first_prompt_is_auto(self):
        found = self._parse(
            [("s1", "/tmp/proj", 1000, 1010, "CloudSQL table comparison", 0)],
            [("check on my cloudsql db and compare two tables", 1000, "s1", 0)],
        )
        self.assertEqual(found["s1"].title_source, "auto")

    def test_inline_rename_matching_title_is_rename(self):
        found = self._parse(
            [
                ("s1", "/tmp/proj", 1000, 1010, "agent-dropdown-sort", 0),
                ("s2", "/tmp/proj", 1000, 1010, "agent-dropdown-sort", 0),
            ],
            [
                ("sort the dropdown", 1000, "s1", 0),
                ("/rename agent-dropdown-sort", 1005, "s1", 0),
                ("sort the dropdown", 1000, "s2", 0),
                ("/rename-chat agent-dropdown-sort", 1005, "s2", 0),
            ],
        )
        self.assertEqual(found["s1"].title_source, "rename")
        self.assertEqual(found["s2"].title_source, "rename")

    def test_interactive_rename_without_argument_under_marks_as_auto(self):
        found = self._parse(
            [("s1", "/tmp/proj", 1000, 1010, "db-conn", 0)],
            [("use gcloud to build a connection string", 1000, "s1", 0), ("/rename-chat ", 1005, "s1", 0)],
        )
        self.assertEqual(found["s1"].title_source, "auto")

    def test_rename_command_is_never_the_title(self):
        found = self._parse(
            [("s1", "/tmp/proj", 1000, 1010, "", 0)],
            [("/rename first", 1000, "s1", 0), ("real prompt", 1001, "s1", 0)],
        )
        self.assertEqual(found["s1"].title, "real prompt")
        self.assertEqual(found["s1"].title_source, "")


class ParseContinueTest(unittest.TestCase):
    def test_reads_sessions_json_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sid = "abc-123"
            (base / "sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "sessionId": sid,
                            "title": "Explore repo",
                            "workspaceDirectory": "/tmp/work",
                            "dateCreated": "2026-07-10T10:00:00.000Z",
                        }
                    ]
                )
            )
            (base / f"{sid}.json").write_text(
                json.dumps({"history": [{"message": {"role": "user", "content": "hello"}}]})
            )

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(base) if p.endswith("sessions") else p,
            ):
                from swe.sessions.parsers import parse_continue

                sessions = parse_continue()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "continue")
            self.assertEqual(sessions[0].path, "/tmp/work")
            self.assertEqual(sessions[0].title, "Explore repo")


class ParseCrushTest(unittest.TestCase):
    def test_reads_projects_json_and_crush_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "projdata"
            data_dir.mkdir()
            db = data_dir / "crush.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT,
                    title TEXT NOT NULL,
                    message_count INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0,
                    updated_at INTEGER,
                    created_at INTEGER,
                    summary_message_id TEXT,
                    todos TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO sessions (id, title, updated_at, created_at) VALUES (?, ?, ?, ?)",
                ("sess1", "Untitled Session", 1777032754, 1777032753),
            )
            conn.commit()
            conn.close()

            projects = Path(tmp) / "projects.json"
            projects.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "path": "/Users/test/Downloads",
                                "data_dir": str(data_dir),
                                "last_accessed": 1777032754,
                            }
                        ]
                    }
                )
            )

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(projects) if p.endswith("projects.json") else p,
            ):
                from swe.sessions.parsers import parse_crush

                sessions = parse_crush()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "crush")
            self.assertEqual(sessions[0].path, "/Users/test/Downloads")
            self.assertEqual(sessions[0].session_id, "sess1")


class ParseCrushFailureTest(unittest.TestCase):
    def test_a_crash_is_recorded_not_swallowed(self):
        # Swallowed, it read as "no Crush history" and the aggregator
        # pruned Crush's cached sessions.
        from swe.sessions import failures
        from swe.sessions.parsers import parse_crush

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects.json"
            projects.write_text("{not json")
            failures.clear()
            try:
                with mock.patch(
                    "swe.sessions.parsers.os.path.expanduser",
                    side_effect=lambda p: str(projects) if p.endswith("projects.json") else p,
                ):
                    self.assertEqual(parse_crush(), [])
                self.assertIn("crush", failures.snapshot())
            finally:
                failures.clear()


class ParseGrokTest(unittest.TestCase):
    def test_reads_encoded_cwd_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            enc = "%2FUsers%2Ftest"
            sess = base / enc / "sess-uuid"
            sess.mkdir(parents=True)
            (sess / "summary.json").write_text(
                json.dumps(
                    {
                        "generated_title": "hello grok",
                        "session_summary": "hello grok",
                        "created_at": "2026-07-06T12:17:43.838414Z",
                        "updated_at": "2026-07-06T12:19:08.646503Z",
                        "last_active_at": "2026-07-06T12:19:08.646503Z",
                    }
                )
            )
            (sess / "prompt_context.json").write_text(
                json.dumps({"working_directory": "/Users/test"})
            )

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(base) if p.endswith("sessions") else p,
            ):
                from swe.sessions.parsers import parse_grok

                sessions = parse_grok()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "grok")
            self.assertEqual(sessions[0].path, "/Users/test")
            self.assertEqual(sessions[0].title, "hello grok")
            self.assertEqual(sessions[0].session_id, "sess-uuid")


class ParseClineTest(unittest.TestCase):
    def test_reads_task_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state = base / "data" / "state"
            state.mkdir(parents=True)
            (state / "taskHistory.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "1777068768553",
                            "ulid": "01KQ0",
                            "ts": 1777068920246,
                            "task": "Hello",
                            "cwdOnTaskInitialization": "/tmp/downloads",
                        }
                    ]
                )
            )

            def expand(p: str) -> str:
                if p.endswith("taskHistory.json"):
                    return str(state / "taskHistory.json")
                if p.endswith("data/sessions"):
                    return str(base / "data" / "sessions")
                return p

            with mock.patch("swe.sessions.parsers.os.path.expanduser", side_effect=expand):
                from swe.sessions.parsers import parse_cline

                sessions = parse_cline()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "cline")
            self.assertEqual(sessions[0].path, "/tmp/downloads")
            self.assertEqual(sessions[0].title, "Hello")

    def test_reads_session_dirs(self):
        # Cline 3.x: data/sessions/<id>/<id>.json beside a
        # <id>.messages.json transcript; only the metadata file counts.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sess_dir = base / "data" / "sessions" / "1789471031317_jp2hv"
            sess_dir.mkdir(parents=True)
            (sess_dir / "1789471031317_jp2hv.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": "1789471031317_jp2hv",
                        "source": "vscode",
                        "started_at": "2026-09-15T11:17:11.371Z",
                        "cwd": "/work/project",
                        "prompt": "hello!",
                        "metadata": {"title": "hello!"},
                    }
                )
            )
            (sess_dir / "1789471031317_jp2hv.messages.json").write_text(
                json.dumps(
                    {
                        "sessionId": "1789471031317_jp2hv",
                        "messages": [{"role": "user", "content": []}],
                    }
                )
            )

            def expand(p: str) -> str:
                if p.endswith("data/sessions"):
                    return str(base / "data" / "sessions")
                if p.endswith("taskHistory.json"):
                    return str(base / "data" / "state" / "taskHistory.json")
                return p

            with mock.patch("swe.sessions.parsers.os.path.expanduser", side_effect=expand):
                from swe.sessions.parsers import parse_cline

                sessions = parse_cline()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "cline")
            self.assertEqual(sessions[0].session_id, "1789471031317_jp2hv")
            self.assertEqual(sessions[0].path, "/work/project")
            self.assertEqual(sessions[0].title, "hello!")
            self.assertGreater(sessions[0].timestamp, 0)

    def test_warm_listing_reads_no_session_file(self):
        settle_cache(self)
        # The glob also matches every <id>.messages.json transcript; inside
        # the parse cache an unchanged file is never opened again, and a
        # changed one is.
        from swe.sessions import parse_cache
        from swe.sessions.parsers import parse_cline

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sess_dir = base / "data" / "sessions" / "s1"
            sess_dir.mkdir(parents=True)
            meta = sess_dir / "s1.json"
            meta.write_text(json.dumps({
                "session_id": "s1", "started_at": "2026-09-15T11:17:11.371Z",
                "cwd": "/work/project", "metadata": {"title": "first"}}))
            (sess_dir / "s1.messages.json").write_text(json.dumps(
                {"sessionId": "s1", "messages": [{"role": "user", "content": "x" * 5000}]}))

            def expand(p: str) -> str:
                if p.endswith("data/sessions"):
                    return str(base / "data" / "sessions")
                if p.endswith("taskHistory.json"):
                    return str(base / "data" / "state" / "taskHistory.json")
                return p

            opened = []
            real_open = open

            def counting_open(path, *args, **kwargs):
                if str(path).startswith(str(sess_dir)):
                    opened.append(str(path))
                return real_open(path, *args, **kwargs)

            def listing():
                with parse_cache.activate(base):
                    sessions = parse_cline()
                    parse_cache.mark_ran("cline")
                return [(s.session_id, s.title, s.path) for s in sessions]

            with mock.patch("swe.sessions.parsers.os.path.expanduser", side_effect=expand), \
                    mock.patch("builtins.open", side_effect=counting_open):
                self.assertEqual(listing(), [("s1", "first", "/work/project")])
                self.assertEqual(len(opened), 2)
                self.assertEqual(listing(), [("s1", "first", "/work/project")])
                self.assertEqual(len(opened), 2)
                meta.write_text(json.dumps({
                    "session_id": "s1", "started_at": "2026-09-15T11:17:11.371Z",
                    "cwd": "/work/project", "metadata": {"title": "renamed"}}))
                self.assertEqual(listing(), [("s1", "renamed", "/work/project")])
                self.assertEqual(len(opened), 3)


class ParseOpenCodeTest(unittest.TestCase):
    def _parse(self, db: Path):
        def expand(p: str) -> str:
            if p.endswith("opencode.db"):
                return str(db)
            return p

        with mock.patch(
            "swe.sessions.parsers.os.path.expanduser", side_effect=expand
        ):
            from swe.sessions.parsers import parse_opencode

            return parse_opencode()

    def test_reads_current_schema_without_workspace_directory(self):
        # opencode 2.x turned `workspace` into provider bindings
        # (id, provider, binding, created_at, last_used_at): no `directory`.
        # Joining on w.directory raised "no such column" and every session
        # vanished; session.directory alone must carry the path.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                "directory TEXT NOT NULL, title TEXT NOT NULL, "
                "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
                "workspace_id TEXT)"
            )
            conn.execute(
                "CREATE TABLE workspace (id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
                "binding TEXT, created_at INTEGER NOT NULL, last_used_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ses_a", "p1", "/work/app", "wire up auth", 1783582327217, 1783582330279, "w1"),
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ses_b", "p1", "/work/lib", "bump deps", 1783582328000, 1783582331000, None),
            )
            conn.execute(
                "INSERT INTO workspace VALUES (?, ?, ?, ?, ?)",
                ("w1", "local", None, 1783582327000, 1783582330000),
            )
            conn.commit()
            conn.close()

            sessions = self._parse(db)
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(set(by_id), {"ses_a", "ses_b"})
        self.assertEqual(by_id["ses_a"].path, "/work/app")
        self.assertEqual(by_id["ses_a"].title, "wire up auth")
        self.assertEqual(by_id["ses_a"].tool_name, "opencode")
        self.assertEqual(by_id["ses_a"].timestamp, 1783582330279)
        self.assertEqual(by_id["ses_b"].path, "/work/lib")

    def test_reads_schema_with_no_workspace_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, "
                "title TEXT, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?)",
                ("ses_c", "/work/x", "hello", 1783582330279),
            )
            conn.commit()
            conn.close()

            sessions = self._parse(db)
        self.assertEqual([s.session_id for s in sessions], ["ses_c"])
        self.assertEqual(sessions[0].path, "/work/x")

    def test_older_schema_falls_back_to_workspace_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, workspace_id TEXT, "
                "directory TEXT, title TEXT, time_updated INTEGER)"
            )
            conn.execute("CREATE TABLE workspace (id TEXT PRIMARY KEY, directory TEXT)")
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                ("ses_d", "w1", "", "old shape", 1783582330279),
            )
            conn.execute("INSERT INTO workspace VALUES (?, ?)", ("w1", "/work/mono"))
            conn.commit()
            conn.close()

            sessions = self._parse(db)
        self.assertEqual([s.path for s in sessions], ["/work/mono"])


class ParseKiloTest(unittest.TestCase):
    def test_reads_opencode_schema_db(self):
        # Kilo is an opencode fork: kilo.db keeps session/workspace with
        # the same drizzle columns — time_updated epoch-ms, directory,
        # and a workspace join for empty directories.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, "
                "workspace_id TEXT, directory TEXT, title TEXT, "
                "time_created INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "CREATE TABLE workspace (id TEXT PRIMARY KEY, directory TEXT)"
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
                ("ses_1", "w1", "", "fix the flake", 1783582327217, 1783582330279),
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
                ("ses_2", None, "/work/app", "add tests", 1783582328000, 1783582331000),
            )
            conn.execute(
                "INSERT INTO workspace VALUES (?, ?)", ("w1", "/work/monorepo")
            )
            conn.commit()
            conn.close()

            def expand(p: str) -> str:
                if p.endswith("kilo.db"):
                    return str(db)
                return p

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser", side_effect=expand
            ):
                from swe.sessions.parsers import parse_kilo

                sessions = parse_kilo()
            self.assertEqual(len(sessions), 2)
            by_id = {s.session_id: s for s in sessions}
            self.assertEqual(by_id["ses_1"].path, "/work/monorepo")
            self.assertEqual(by_id["ses_1"].title, "fix the flake")
            self.assertEqual(by_id["ses_1"].tool_name, "kilo")
            self.assertEqual(by_id["ses_1"].timestamp, 1783582330279)
            self.assertEqual(by_id["ses_2"].path, "/work/app")

    def test_missing_db_returns_empty(self):
        with mock.patch(
            "swe.sessions.parsers.os.path.expanduser",
            side_effect=lambda p: "/nonexistent/kilo.db" if "kilo" in p else p,
        ):
            from swe.sessions.parsers import parse_kilo

            self.assertEqual(parse_kilo(), [])


class ParseForgeTest(unittest.TestCase):
    def test_reads_conversations_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / ".forge.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    workspace_id INTEGER,
                    context TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    metrics TEXT
                )
                """
            )
            ctx = json.dumps(
                {
                    "messages": [
                        {
                            "message": {
                                "text": {
                                    "role": "User",
                                    "content": "<task>fix the build</task>",
                                }
                            }
                        }
                    ]
                }
            )
            conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "c1",
                    "Build Fix",
                    1,
                    ctx,
                    "2026-07-01 10:00:00",
                    "2026-07-01 11:00:00",
                    None,
                ),
            )
            conn.commit()
            conn.close()

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(db) if p.endswith(".forge.db") else ("/Users/test" if p == "~" else p),
            ):
                from swe.sessions.parsers import parse_forge

                sessions = parse_forge()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].tool_name, "forge")
            self.assertEqual(sessions[0].title, "Build Fix")
            self.assertEqual(sessions[0].session_id, "c1")


class TrackedCountsTest(unittest.TestCase):
    def test_session_counts_includes_zero_for_tracked(self):
        from swe.sessions.usage import session_counts_100d, tracked_tool_names

        from swe.sessions.models import Session

        tracked = tracked_tool_names()
        self.assertIn("copilot", tracked)
        self.assertIn("grok", tracked)
        self.assertIn("cline", tracked)
        now_ms = time.time() * 1000
        old_ms = (time.time() - 200 * 86400) * 1000
        # Stub the scan and the cache: unpatched, this walks every
        # transcript under the real HOME and rewrites the counts cache.
        sessions = [
            Session(timestamp=now_ms, agent="Copilot", path="/p", tool_name="copilot"),
            Session(timestamp=now_ms, agent="Copilot", path="/p", tool_name="copilot"),
            Session(timestamp=old_ms, agent="Grok", path="/p", tool_name="grok"),
        ]
        with mock.patch("swe.sessions.usage.get_all_sessions", return_value=sessions), \
                mock.patch("swe.sessions.usage._load_cached_counts", return_value=None), \
                mock.patch("swe.sessions.usage._save_cached_counts") as save, \
                mock.patch("swe.sessions.aggregator._load_cached_sessions", return_value=None):
            counts = session_counts_100d()
        self.assertEqual(set(counts), tracked)
        self.assertEqual(counts["copilot"], 2)
        self.assertEqual(counts["grok"], 0)  # outside the 100-day window
        self.assertEqual(counts["cline"], 0)
        save.assert_called_once()


class ParseCrushEdgeTest(unittest.TestCase):
    def _expand(self, projects, data_dir=None):
        def expand(path):
            if path.endswith("projects.json"):
                return str(projects)
            if data_dir and "crush" in path and path.startswith("~"):
                return str(Path(data_dir))
            return path
        return expand

    def test_missing_projects_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=self._expand(missing),
            ):
                from swe.sessions.parsers import parse_crush
                self.assertEqual(parse_crush(), [])

    def test_non_list_projects_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects.json"
            projects.write_text(json.dumps({"projects": "junk"}))
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=self._expand(projects),
            ):
                from swe.sessions.parsers import parse_crush
                self.assertEqual(parse_crush(), [])

    def test_project_without_db_falls_back_to_last_accessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects.json"
            projects.write_text(json.dumps({"projects": [
                {"path": "/w", "data_dir": str(Path(tmp) / "nodb"),
                 "last_accessed": 1777032754},
                {"path": "", "data_dir": ""},   # skipped: missing fields
                "not a dict",
            ]}))
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=self._expand(projects),
            ):
                from swe.sessions.parsers import parse_crush
                sessions = parse_crush()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].path, "/w")
            self.assertEqual(sessions[0].tool_name, "crush")


class CodexThreadStoreTest(unittest.TestCase):
    def test_no_store_returns_empty_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(Path(tmp) / ".codex" / p[7:]) if p.startswith("~/.codex") else p,
            ):
                from swe.sessions.parsers import _codex_thread_store
                self.assertEqual(_codex_thread_store(), {})

    def test_store_schema_without_threads_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state_5.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE other (id TEXT)")
            conn.commit()
            conn.close()
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(Path(tmp) / p[9:]) if p.startswith("~/.codex/") else p,
            ):
                from swe.sessions.parsers import _codex_thread_store
                self.assertEqual(_codex_thread_store(), {})

    def test_reads_names_and_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state_5.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT, preview TEXT)")
            conn.execute("INSERT INTO threads VALUES ('ABC', 'My Title', 'first msg')")
            conn.execute("INSERT INTO threads VALUES ('  ', 'blank', 'x')")
            conn.execute("INSERT INTO threads VALUES (NULL, 'noid', 'x')")
            conn.commit()
            conn.close()
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(Path(tmp) / p[9:]) if p.startswith("~/.codex/") else p,
            ):
                from swe.sessions.parsers import _codex_thread_store
                store = _codex_thread_store()
            self.assertEqual(store, {"abc": ("My Title", "first msg", False, "")})


class CursorMetaTest(unittest.TestCase):
    def test_missing_chats_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(Path(tmp) / "chats") if "chats" in p else p,
            ):
                from swe.sessions.parsers import _cursor_meta
                self.assertEqual(_cursor_meta("s1"), {})

    def test_reads_meta_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "chats" / "hash1" / "s1" / "meta.json"
            meta.parent.mkdir(parents=True)
            meta.write_text(json.dumps({"cwd": "/w", "title": "T"}))
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(Path(tmp) / "chats") if "chats" in p else p,
            ):
                from swe.sessions.parsers import _cursor_meta
                self.assertEqual(_cursor_meta("s1"), {"cwd": "/w", "title": "T"})
                self.assertEqual(_cursor_meta("other"), {})
                self.assertEqual(_cursor_meta(""), {})


class ParseFreebuffEnrichTest(unittest.TestCase):
    def test_run_state_overrides_path_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "manicode" / "projects"
            sess = base / "proj" / "chats" / "sess-1"
            sess.mkdir(parents=True)
            (sess / "log.jsonl").write_text(
                json.dumps({"role": "user", "content": "log line"}) + "\n")
            (sess / "run-state.json").write_text(json.dumps({
                "sessionState": {"mainAgentState": {
                    "cwd": "/real/work",
                    "messageHistory": [
                        {"role": "assistant", "content": "skip"},
                        {"role": "user", "content": "state title"},
                    ],
                }}
            }))
            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser",
                side_effect=lambda p: str(base) + "/" if "manicode" in p else p,
            ):
                from swe.sessions.parsers import parse_freebuff
                sessions = parse_freebuff()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].path, "/real/work")
            self.assertEqual(sessions[0].title, "state title")


class ParseGeminiEnrichTest(unittest.TestCase):
    def test_projects_map_and_logs_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "projects.json"
            index.write_text(json.dumps({"projects": {"/w/p": "h1"}}))
            logdir = root / "tmp" / "h1"
            logdir.mkdir(parents=True)
            (logdir / "logs.json").write_text(json.dumps([
                {"type": "system", "message": "skip"},
                {"type": "user", "message": "  gemini prompt  "},
            ]))

            def expand(p):
                if p.endswith("projects.json"):
                    return str(index)
                if "gemini/tmp" in p:
                    return str(root / "tmp" / p.split("/")[-1])
                return p

            with mock.patch(
                "swe.sessions.parsers.os.path.expanduser", side_effect=expand):
                from swe.sessions.parsers import parse_gemini
                sessions = parse_gemini()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].title, "gemini prompt")


if __name__ == "__main__":
    unittest.main()
