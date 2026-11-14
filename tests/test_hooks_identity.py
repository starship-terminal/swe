"""Which hook a run was: ``hooks/identity.py`` end to end.

Every test builds a real inventory in a throwaway home (hub scripts, links
into ``~/.claude/hooks``, declarations in ``~/.claude/settings.json``),
writes the hook log and a Claude Code transcript, and reads the answer
through the real ``swe hooks stats --json`` and ``swe hooks log --json``.
The join a client makes is ``source_paths`` on a stats row against
``source_path`` on a ``swe hooks list --json`` row, so that is what the
tests check, never a name.
"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from swe import paths
from swe.hooks import shared as _shared
from swe.hooks import shim_runtime

from tests._tempdir import temp_dir
from tests.test_hooks_stats import _rec, _run

HOUR = 3600


class _IdentityHome(unittest.TestCase):
    """A home with Claude Code installed and nothing else yet."""

    home_name = "home"

    def setUp(self):
        self.home = temp_dir(self) / self.home_name
        (self.home / ".claude" / "hooks").mkdir(parents=True)
        (self.home / ".swe").mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWE_HOOK_LOG", None)
        self.now = time.time() - 600
        self.lines: list[str] = []
        self.log_rows: list[dict] = []
        self.decls: dict[str, list[str]] = {}

    # -- the inventory ------------------------------------------------------

    def hub(self, rel: str, body: str = "print('ok')\n") -> Path:
        """``~/.swe/hooks/claude/<rel>``, linked into ``~/.claude/hooks``
        the way ``swe init`` links its first path part."""
        path = self.home / ".swe" / "hooks" / "claude" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        top = rel.split("/", 1)[0]
        dest = self.home / ".claude" / "hooks" / top
        if not dest.is_symlink():
            dest.symlink_to(self.home / ".swe" / "hooks" / "claude" / top)
        return self.home / ".claude" / "hooks" / rel

    def foreign(self, rel: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        return path

    def shared(self, name: str) -> Path:
        """A deployed shared hook: the script, its wrapper and the link."""
        source = _shared.shared_dir(self.home) / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# events: [Stop]\nprint('{}')\n")
        with mock.patch.object(Path, "home", staticmethod(lambda: self.home)):
            _shared.apply_deploy(self.home, {})
        link = self.home / ".claude" / "hooks" / "_generated"
        if not link.is_symlink():
            link.symlink_to(_shared.generated_dir(self.home, "claude"))
        return link / name

    def declare(self, command: str, event: str = "Stop") -> str:
        self.decls.setdefault(event, []).append(command)
        path = self.home / ".claude" / "settings.json"
        path.write_text(json.dumps({"hooks": {
            ev: [{"hooks": [{"type": "command", "command": c} for c in cmds]}]
            for ev, cmds in self.decls.items()}}))
        return command

    # -- the runs -----------------------------------------------------------

    def ran(self, command: str, ago: float, session: str = "s1",
            event: str = "Stop", stderr: str = "") -> None:
        """One run in the Claude transcript (a ``hook_success``)."""
        self.lines.append(_rec(self.now - ago, "attachment", sessionId=session,
                               attachment={
            "type": "hook_success", "hookName": event,
            "hookEvent": event.split(":")[0], "command": command,
            "exitCode": 0, "durationMs": 20, "stderr": stderr, "stdout": ""}))
        self._write_transcript()

    def blocked(self, command: str, ago: float, reason: str,
                session: str = "s1") -> None:
        """One Stop block in the transcript (the feedback message)."""
        self.lines.append(_rec(self.now - ago, "user", isMeta=True,
                               sessionId=session, message={
            "role": "user",
            "content": f"Stop hook feedback:\n[{command}]: {reason}"}))
        self._write_transcript()

    def _write_transcript(self) -> None:
        project = self.home / ".claude" / "projects" / "-w-app"
        project.mkdir(parents=True, exist_ok=True)
        (project / "s.jsonl").write_text("\n".join(self.lines) + "\n")

    def logged(self, hook: str, ago: float, *, script=None, session="s1",
               harness="claude", event="Stop", decision="allow",
               reason="") -> None:
        """One hook log line; ``script=None`` writes the format from before
        the field existed."""
        row = {"v": 1, "ts": self.now - ago, "hook": hook, "harness": harness,
               "event": event, "session_id": session, "cwd": "/w/app",
               "repo": None, "git_branch": "main", "decision": decision,
               "reason": reason, "duration_ms": 10, "exit_code": 0,
               "stop_hook_active": False}
        if script is not None:
            row["script"] = str(script)
        self.log_rows.append(row)
        log = paths.hook_events_file_for(self.home)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(json.dumps(r) + "\n" for r in self.log_rows))

    # -- the answers --------------------------------------------------------

    def swe(self, *args: str) -> dict:
        code, out, err = _run(self.home, *args, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def stats(self, *flags: str) -> dict:
        return self.swe("stats", "--sum-harnesses", *flags)

    def listed(self) -> dict[str, list[dict]]:
        """``swe hooks list --json`` rows by name."""
        out: dict[str, list[dict]] = {}
        for row in self.swe("list")["hooks"]:
            out.setdefault(row["name"], []).append(row)
        return out

    def source(self, name: str) -> str:
        (row, *rest) = self.listed()[name]
        self.assertTrue(all(r["source_path"] == row["source_path"] for r in rest))
        return row["source_path"]

    def counted(self, source: str, data: dict | None = None) -> dict | None:
        """The one stats row a list row's ``source_path`` joins to."""
        data = data or self.stats()
        rows = [r for r in data["hooks"] if source in r["source_paths"]]
        self.assertLessEqual(len(rows), 1, rows)
        return rows[0] if rows else None

    def fires(self, source: str, data: dict | None = None) -> int:
        row = self.counted(source, data)
        return row["fires"] if row else 0


class SharedHookTest(_IdentityHome):
    """The shim logs a shared hook as ``notify``; Claude's transcript sees
    its wrapper ``~/.claude/hooks/_generated/notify.py``. One run."""

    def setUp(self):
        super().setUp()
        self.wrapper = self.shared("notify.py")
        self.command = self.declare(f'python3 "{self.wrapper}"')

    def shim_run(self) -> float:
        """Run the deployed wrapper for real; the log line's timestamp."""
        proc = subprocess.run(
            [sys.executable, str(self.wrapper)],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "s1",
                              "cwd": "/w/app"}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = paths.hook_events_file_for(self.home)
        (line,) = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertEqual(line["hook"], "notify")
        self.assertEqual(line["script"], os.path.realpath(
            _shared.shared_dir(self.home) / "notify.py"))
        return line["ts"]

    def test_the_helper_and_transcript_copies_count_once(self):
        ts = self.shim_run()
        self.now = ts - 0.5     # the transcript lands half a second later
        self.ran(self.command, 0)
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 1)
        self.assertEqual(data["sources"]["duplicates"], 1)
        (row,) = data["hooks"]
        self.assertEqual(row["hook"], "notify")
        self.assertEqual(row["source_paths"], [self.source("notify.py")])
        # The panel's runs: one, not two.
        runs = self.swe("log", f"--source={self.source('notify.py')}")
        self.assertEqual(runs["matched"], 1)

    def test_a_log_line_from_before_the_script_field_still_counts_once(self):
        self.logged("notify", HOUR)
        self.ran(self.command, HOUR - 1)
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 1)
        self.assertEqual(self.fires(self.source("notify.py"), data), 1)

    def test_a_run_through_a_wrapper_gone_since_still_counts_once(self):
        ts = self.shim_run()
        self.now = ts - 0.5
        self.ran(self.command, 0)
        # The wrapper is pruned afterwards; the script and the declaration
        # stay. The transcript's command now points at nothing, but it is
        # still the shared script's run.
        Path(os.path.realpath(self.wrapper)).unlink()
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 1)
        self.assertEqual(self.fires(self.source("notify.py"), data), 1)

    def test_a_transcript_run_is_named_like_the_log(self):
        self.ran(self.command, HOUR)
        (row,) = self.stats()["hooks"]
        self.assertEqual(row["hook"], "notify")
        self.assertNotIn(".claude/notify", json.dumps(self.stats()))


class DirectoryHookTest(_IdentityHome):
    """``bundle`` is listed by its folder; its runs carry its executables."""

    def test_one_declared_executable(self):
        guard = self.hub("bundle/guard.py")
        # A helper inside the folder that nothing declares, and a separate
        # hook with the helper's name: the helper claims none of its runs.
        self.hub("bundle/lib/prose-guard.py")
        pg = self.hub("prose-guard.py")
        cmd = self.declare(f'python3 "{guard}"')
        pg_cmd = self.declare(f'python3 "{pg}"')
        for i in range(3):
            self.ran(cmd, HOUR + i, session=f"g{i}")
        for i in range(2):
            self.ran(pg_cmd, 2 * HOUR + i, session=f"p{i}")
        # Old log lines named after the helper's stem belong to the hook
        # whose file has it, not the folder holding a copy.
        self.logged("prose-guard", 3 * HOUR, harness="codex", session="c")
        data = self.stats()
        self.assertEqual(self.fires(self.source("bundle"), data), 3)
        self.assertEqual(self.fires(self.source("prose-guard.py"), data), 3)
        self.assertEqual(self.counted(self.source("bundle"), data)["hook"],
                         "bundle")
        self.assertEqual(data["uncertain"], [])

    def test_two_declared_executables(self):
        guard = self.hub("bundle/guard.py")
        stop = self.hub("bundle/scripts/on-stop.sh")
        g = self.declare(f'python3 "{guard}"', "PreToolUse")
        s = self.declare(f'bash "{stop}"')
        self.ran(g, 3 * HOUR, event="PreToolUse:Bash")
        self.ran(g, 2 * HOUR, event="PreToolUse:Bash")
        self.blocked(s, HOUR, "stop-rule: no")
        data = self.stats()
        row = self.counted(self.source("bundle"), data)
        self.assertEqual((row["fires"], row["blocks"]), (3, 1))
        self.assertEqual(row["top_reasons"], [
            {"decision": "block", "reason": "stop-rule: no", "count": 1}])
        self.assertEqual(sorted(row["scripts"]), sorted(
            os.path.realpath(p) for p in (guard, stop)))
        runs = self.swe("log", f"--source={self.source('bundle')}")
        self.assertEqual(runs["matched"], 3)
        self.assertEqual(runs["hook"], "bundle")
        self.assertEqual(runs["events"][0]["decision"], "block")
        # The CLI name of the row finds the same runs.
        self.assertEqual(self.swe("log", "bundle")["matched"], 3)

    def test_an_undeclared_helper_claims_nothing(self):
        self.hub("bundle/guard.py")
        helper = self.hub("bundle/lib/check.py")
        # The helper ran from a command no settings file declares: it is
        # not the folder's executable, so the folder does not get the run.
        self.ran(f'python3 "{helper}"', HOUR)
        self.logged("check", 2 * HOUR, harness="codex", session="c")
        data = self.stats()
        self.assertIsNone(self.counted(self.source("bundle"), data))
        self.assertEqual(data["totals"]["fires"], 2)

    def test_a_hook_that_records_itself_by_its_bare_stem(self):
        stop = self.hub("bundle/on-stop.py")
        cmd = self.declare(f'python3 "{stop}"')
        # The transcript names it bundle/on-stop (a generic stem takes its
        # folder); the hook's own record() call logs on-stop, and from
        # this version on the file that ran.
        with mock.patch.object(sys, "argv", [str(stop)]):
            self.assertTrue(shim_runtime.record(
                "on-stop", "allow", harness="claude", event="Stop",
                session_id="s1", cwd="/w/app", ts=self.now - HOUR))
        line = json.loads(paths.hook_events_file_for(self.home).read_text())
        self.log_rows.append(line)
        self.ran(cmd, HOUR - 1)
        # An older line with no script, from a harness with no transcript.
        self.logged("on-stop", 2 * HOUR, harness="codex", session="c")
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 2)
        self.assertEqual(self.fires(self.source("bundle"), data), 2)


class SameBasenameTest(_IdentityHome):
    """Two ``guard.sh`` files in different folders are two hooks."""

    def setUp(self):
        super().setUp()
        self.a = self.foreign("plugins/a/guard.sh")
        self.b = self.foreign("plugins/b/guard.sh")
        self.cmd_a = self.declare(f"bash {self.a}")
        self.declare(f"bash {self.b}")

    def test_they_never_share_counts(self):
        for i in range(3):
            self.ran(self.cmd_a, HOUR + i, session=f"a{i}")
        data = self.stats()
        self.assertEqual(self.fires(str(self.a), data), 3)
        self.assertIsNone(self.counted(str(self.b), data))

    def test_a_run_that_could_be_either_is_neither_and_says_so(self):
        self.ran(self.cmd_a, HOUR)
        self.logged("guard", 2 * HOUR, harness="codex", session="c")
        data = self.stats()
        self.assertEqual(self.fires(str(self.a), data), 1)
        self.assertIsNone(self.counted(str(self.b), data))
        self.assertEqual(data["uncertain"], sorted([str(self.a), str(self.b)]))


    def test_one_stop_feedback_is_the_block_of_its_own_script(self):
        # Both ran at one Stop and a blocked: the transcript holds a summary
        # entry for each and a's feedback. The feedback absorbs a's entry,
        # never b's, though the two log under the one name.
        self.lines.append(_rec(self.now - HOUR, "system", sessionId="s1",
                               subtype="stop_hook_summary", hookErrors=[],
                               hookInfos=[{"command": f"bash {self.b}",
                                           "durationMs": 6},
                                          {"command": f"bash {self.a}",
                                           "durationMs": 5}]))
        self.blocked(self.cmd_a, HOUR - 1, "a says no")
        data = self.stats()
        a, b = self.counted(str(self.a), data), self.counted(str(self.b), data)
        self.assertEqual((a["fires"], a["blocks"]), (1, 1))
        self.assertEqual((b["fires"], b["blocks"]), (1, 0))


class PerHarnessCopyTest(_IdentityHome):
    """``~/.swe/hooks/claude/x.py`` and ``~/.swe/hooks/codex/x.py`` are two
    scripts with one stem; a log line from codex is the codex one's."""

    def test_an_old_line_goes_to_the_copy_on_its_harness(self):
        self.hub("prose-guard.py")
        codex = self.home / ".swe" / "hooks" / "codex" / "prose-guard.py"
        codex.parent.mkdir(parents=True)
        codex.write_text("print(1)\n")
        (self.home / ".codex").mkdir()
        self.logged("prose-guard", HOUR, harness="codex", session="c")
        rows = {r["harness"]: r["source_path"]
                for r in self.listed()["prose-guard.py"]}
        self.assertNotEqual(rows["claude"], rows["codex"])
        data = self.stats()
        self.assertEqual(self.fires(rows["codex"], data), 1)
        self.assertIsNone(self.counted(rows["claude"], data))
        self.assertEqual(data["uncertain"], [])


class QuotedSpaceTest(_IdentityHome):
    """``python3 "/Users/Jane Doe/.claude/hooks/bundle/guard.py"``."""

    home_name = "Jane Doe"

    def test_a_quoted_path_with_a_space_keeps_its_counts(self):
        guard = self.hub("bundle/guard.py")
        self.assertIn(" ", str(guard))
        cmd = self.declare(f'python3 "{guard}"')
        self.ran(cmd, HOUR)
        self.ran(cmd, 2 * HOUR, session="s2")
        row = self.counted(self.source("bundle"))
        self.assertEqual(row["fires"], 2)
        self.assertEqual(row["scripts"], [os.path.realpath(guard)])


class PluginRootTest(_IdentityHome):
    """A ``${CLAUDE_PLUGIN_ROOT}`` command has no path swe can know."""

    PLUGIN = "${CLAUDE_PLUGIN_ROOT}/hooks/on-stop.sh"

    def test_its_name_counts_once_and_never_for_another_hook(self):
        plugin_file = self.foreign(".claude/plugins/x/hooks/on-stop.sh")
        self.declare(str(plugin_file))
        self.declare(self.PLUGIN)
        wrapper = self.shared("on-stop.py")
        shared_cmd = self.declare(f'python3 "{wrapper}"')
        # One Stop: the plugin and the shared on-stop both ran; the shim
        # logged its own run, the transcript saw both.
        self.logged("on-stop", HOUR, script=os.path.realpath(
            _shared.shared_dir(self.home) / "on-stop.py"))
        self.ran(self.PLUGIN, HOUR - 0.2)
        self.ran(shared_cmd, HOUR - 0.4)
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 2)
        self.assertEqual(data["sources"]["duplicates"], 1)
        self.assertEqual(self.fires(self.source("on-stop.py"), data), 1)
        self.assertEqual(self.fires(str(plugin_file), data), 1)
        names = {r["hook"] for r in data["hooks"]}
        self.assertEqual(names, {"on-stop"})

    def test_a_plugin_run_with_no_row_stays_its_own(self):
        self.ran(self.PLUGIN, HOUR)
        self.logged("on-stop", 2 * HOUR)
        data = self.stats()
        rows = {r["hook"]: r for r in data["hooks"]}
        self.assertEqual(set(rows), {"on-stop", "claude-plugin/on-stop"})
        self.assertEqual([r["source_paths"] for r in data["hooks"]], [[], []])
        self.assertEqual(self.swe("log", "claude-plugin/on-stop")["matched"], 1)


class HarnessFilterTest(_IdentityHome):
    def test_rows_join_the_same_way_under_any_harness_filter(self):
        a = self.foreign("plugins/a/guard.sh")
        self.declare(f"bash {a}")
        self.foreign("plugins/b/guard.sh")
        self.declare(f"bash {self.home}/plugins/b/guard.sh")
        pg = self.hub("prose-guard.py")
        self.ran(f"bash {a}", HOUR)
        self.ran(f'python3 "{pg}"', 2 * HOUR)
        self.logged("prose-guard", 3 * HOUR, harness="codex", session="c")
        self.logged("guard", 4 * HOUR, harness="codex", session="d")
        every = self.stats()
        claude = self.stats("--harness=claude")
        codex = self.stats("--harness=codex")
        join = lambda d: {(tuple(r["source_paths"]), r["fires"])
                          for r in d["hooks"]}
        self.assertEqual(join(claude), {((str(a),), 1),
                                        ((self.source("prose-guard.py"),), 1)})
        self.assertIn(((self.source("prose-guard.py"),), 2), join(every))
        self.assertIn(((self.source("prose-guard.py"),), 1), join(codex))
        # The codex guard line could be either guard.sh under any filter.
        self.assertEqual(codex["uncertain"], every["uncertain"])
        self.assertEqual(claude["uncertain"], [])


class OldFormatTest(_IdentityHome):
    """Log lines written before ``script`` existed still count."""

    def test_old_lines_join_by_the_file_stem(self):
        self.hub("prose-guard.py")
        for i in range(4):
            self.logged("prose-guard", HOUR + i, harness="codex",
                        session=f"c{i}", decision="block" if i else "allow",
                        reason="rule")
        row = self.counted(self.source("prose-guard.py"))
        self.assertEqual((row["fires"], row["blocks"]), (4, 3))
        self.assertEqual(row["scripts"], [])

    def test_old_lines_with_no_inventory_group_by_name(self):
        self.logged("x", HOUR)
        self.logged("x", 2 * HOUR, session="s2")
        (row,) = self.stats()["hooks"]
        self.assertEqual((row["hook"], row["fires"], row["source_paths"]),
                         ("x", 2, []))

    def test_a_line_with_a_relative_script_is_read_as_old(self):
        self.hub("prose-guard.py")
        self.logged("prose-guard", HOUR, script="prose-guard.py")
        self.assertEqual(self.fires(self.source("prose-guard.py")), 1)


class ReviewRoundFourTest(_IdentityHome):
    """Greptile's fourth round on #1928: an owner that was never
    registered, an owner claimed without checking, recovery across one
    folder's scripts, and a stats name that misses its row's runs."""

    def record_as(self, path, hook, ago, session, harness="codex"):
        with mock.patch.object(sys, "argv", [str(path)]):
            self.assertTrue(shim_runtime.record(
                hook, "allow", harness=harness, event="Stop",
                session_id=session, cwd="/w/app", ts=self.now - ago))
        log = paths.hook_events_file_for(self.home)
        self.log_rows = [json.loads(x) for x in log.read_text().splitlines()]

    def test_one_declaration_running_two_executables_registers_both(self):
        a = self.hub("bundle/a.py")
        b = self.hub("bundle/b.py")
        cmd = self.declare(f'python3 "{a}" && python3 "{b}"')
        self.record_as(a, "a", HOUR, "c1")
        self.record_as(b, "b", 2 * HOUR, "c2")
        # The transcript run of the whole command is one run with no single
        # script; it still belongs to the folder.
        self.ran(cmd, 3 * HOUR, session="t1")
        data = self.stats()
        self.assertEqual(self.fires(self.source("bundle"), data), 3)
        self.assertEqual(self.swe("log", f"--source={self.source('bundle')}")
                         ["matched"], 3)

    def test_a_foreign_file_at_the_wrapper_path_is_not_the_shared_hook(self):
        source = _shared.shared_dir(self.home) / "notify.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# events: [Stop]\nprint('{}')\n")
        # Another installer's own _generated folder sits where swe would
        # link its wrappers, with a notify.py of its own.
        theirs = self.home / ".claude" / "hooks" / "_generated" / "notify.py"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("print('theirs')\n")
        with mock.patch.object(Path, "home", staticmethod(lambda: self.home)):
            _shared.apply_deploy(self.home, {})
        cmd = self.declare(f'python3 "{theirs}"')
        self.ran(cmd, HOUR)
        self.ran(cmd, 2 * HOUR, session="s2")
        data = self.stats()
        self.assertIsNone(self.counted(self.source("notify.py"), data))
        self.assertEqual(data["totals"]["fires"], 2)

    def test_recovery_is_per_script_inside_one_folder(self):
        x = self.hub("bundle/x.sh", "#!/bin/sh\n")
        y = self.hub("bundle/y.sh", "#!/bin/sh\n")
        cx, cy = self.declare(f"bash {x}"), self.declare(f"bash {y}")
        # x blocks, then y (which never blocked) allows: no recovery.
        self.blocked(cx, 2 * HOUR, "x says no")
        self.ran(cy, HOUR)
        row = self.counted(self.source("bundle"))
        self.assertEqual((row["fires"], row["blocks"], row["recovered"]),
                         (2, 1, 0))
        # x itself allows next: that is its recovery.
        self.ran(cx, HOUR - 60)
        self.assertEqual(self.counted(self.source("bundle"))["recovered"], 1)

    def test_an_unresolvable_declaration_still_registers_its_stem(self):
        # The folder's executable is declared through a variable swe cannot
        # resolve; the inventory still joins it to the folder by name, so
        # an old log line named after the executable is the folder's, just
        # as the transcript's run of that command is.
        self.hub("bundle/guard.py")
        cmd = self.declare('python3 "$HOOKS_DIR/bundle/guard.py"')
        self.logged("guard", HOUR, harness="codex", session="c")
        self.ran(cmd, 2 * HOUR, session="t")
        data = self.stats()
        self.assertEqual(self.fires(self.source("bundle"), data), 2)
        self.assertEqual(data["uncertain"], [])

    def test_a_stats_name_finds_every_run_its_row_counts(self):
        # A script no listed row owns, logged under two names: its own
        # record() call says on-stop, the transcript tools/on-stop.
        script = self.foreign("tools/on-stop.py")
        self.record_as(script, "on-stop", HOUR, "c1")
        self.ran(f"python3 {script}", 2 * HOUR, session="t1")
        (row,) = self.stats()["hooks"]
        self.assertEqual((row["hook"], row["fires"]), ("on-stop", 2))
        self.assertEqual(self.swe("log", row["hook"])["matched"], 2)
        (only,) = self.stats(f"--hook={row['hook']}")["hooks"]
        self.assertEqual(only["fires"], 2)


class KnowableVariablesTest(_IdentityHome):
    """``${CLAUDE_PLUGIN_ROOT}`` and ``$CLAUDE_PROJECT_DIR`` resolved where
    Claude Code leaves enough on disk or in the transcript to know them."""

    PLUGIN = '"${CLAUDE_PLUGIN_ROOT}/hooks/on-stop.sh"'

    def install_plugin(self, name: str, commands: list[str]) -> Path:
        folder = self.home / ".claude" / "plugins" / "cache" / "mk" / name / "1.0.0"
        (folder / "hooks").mkdir(parents=True)
        (folder / "hooks" / "on-stop.sh").write_text("#!/bin/sh\n")
        (folder / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": c}
                                for c in commands]}]}}))
        record = self.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(record.read_text()) if record.exists() else {
            "version": 2, "plugins": {}}
        data["plugins"][f"{name}@mk"] = [{"scope": "user",
                                          "installPath": str(folder),
                                          "version": "1.0.0"}]
        record.write_text(json.dumps(data))
        return folder / "hooks" / "on-stop.sh"

    def test_a_plugin_command_resolves_to_the_plugin_that_declares_it(self):
        # A hub hook of the same basename, joined to the plugin command by
        # name: before, the plugin's runs were the hub hook's.
        self.hub("on-stop.sh", "#!/bin/sh\n")
        self.declare(self.PLUGIN)
        script = self.install_plugin("x", [self.PLUGIN])
        self.ran(self.PLUGIN, HOUR)
        self.ran(self.PLUGIN, 2 * HOUR, session="s2")
        data = self.stats()
        self.assertIsNone(self.counted(self.source("on-stop.sh"), data))
        (row,) = data["hooks"]
        self.assertEqual((row["fires"], row["scripts"], row["source_paths"]),
                         (2, [os.path.realpath(script)], []))

    def test_two_plugins_with_one_command_stay_unknowable(self):
        self.install_plugin("x", [self.PLUGIN])
        self.install_plugin("y", [self.PLUGIN])
        self.ran(self.PLUGIN, HOUR)
        (row,) = self.stats()["hooks"]
        self.assertEqual((row["hook"], row["scripts"]),
                         ("claude-plugin/on-stop", []))

    def test_the_project_dir_is_the_sessions_first_cwd(self):
        self.hub("check.py")
        cmd = self.declare('python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/check.py"')
        project = self.home / "w" / "app"
        (project / ".claude" / "hooks").mkdir(parents=True)
        (project / ".claude" / "hooks" / "check.py").write_text("print(1)\n")
        self.lines.append(_rec(self.now - 3 * HOUR, "user", cwd=str(project),
                               message={"role": "user", "content": "hi"}))
        self.ran(cmd, HOUR)
        data = self.stats()
        # The hub's check.py is another file: it gets none of these runs.
        self.assertIsNone(self.counted(self.source("check.py"), data))
        (row,) = data["hooks"]
        self.assertEqual(row["scripts"], [os.path.realpath(
            project / ".claude" / "hooks" / "check.py")])

    def test_an_unknowable_name_two_rows_could_own_is_uncertain(self):
        a = self.foreign("plugins/a/guard.sh")
        b = self.foreign("plugins/b/guard.sh")
        self.declare(f"bash {a}")
        self.declare(f"bash {b}")
        cmd = self.declare('bash "$SOME_TOOL_DIR/guard.sh"')
        self.ran(cmd, HOUR)
        data = self.stats()
        self.assertIsNone(self.counted(str(a), data))
        self.assertIsNone(self.counted(str(b), data))
        self.assertEqual(data["uncertain"], sorted([str(a), str(b)]))

    def test_an_unknowable_name_one_row_can_own_stays_its(self):
        a = self.foreign("plugins/a/guard.sh")
        self.declare(f"bash {a}")
        cmd = self.declare('bash "$SOME_TOOL_DIR/guard.sh"')
        self.ran(cmd, HOUR)
        data = self.stats()
        self.assertEqual(self.fires(str(a), data), 1)
        self.assertEqual(data["uncertain"], [])


class VanishedGeneratedFolderTest(_IdentityHome):
    """A past run through ``~/.claude/hooks/_generated/<name>`` after the
    whole folder link is gone still meets the shim's copy of it."""

    def test_the_run_counts_once(self):
        wrapper = self.shared("notify.py")
        cmd = self.declare(f'python3 "{wrapper}"')
        shared = os.path.realpath(_shared.shared_dir(self.home) / "notify.py")
        self.logged("notify", HOUR, script=shared)
        self.ran(cmd, HOUR - 0.5)
        (self.home / ".claude" / "hooks" / "_generated").unlink()
        data = self.stats()
        self.assertEqual(data["totals"]["fires"], 1)
        self.assertEqual(data["sources"]["duplicates"], 1)
        (row,) = data["hooks"]
        self.assertEqual((row["hook"], row["scripts"]), ("notify", [shared]))

    def test_a_file_at_that_path_is_never_taken_for_the_wrapper(self):
        wrapper = self.shared("notify.py")
        cmd = self.declare(f'python3 "{wrapper}"')
        link = self.home / ".claude" / "hooks" / "_generated"
        link.unlink()
        link.mkdir()
        (link / "notify.py").write_text("print('theirs')\n")
        shared = os.path.realpath(_shared.shared_dir(self.home) / "notify.py")
        self.logged("notify", HOUR, script=shared)
        self.ran(cmd, HOUR - 0.5)
        self.assertEqual(self.stats()["totals"]["fires"], 2)

    def test_no_shared_script_of_that_name_means_no_match(self):
        cmd = f'python3 "{self.home}/.claude/hooks/_generated/gone.py"'
        self.logged("gone", HOUR, script=str(self.home / ".swe/hooks/shared/gone.py"))
        self.ran(cmd, HOUR - 0.5)
        self.assertEqual(self.stats()["totals"]["fires"], 2)


class ResolverTest(_IdentityHome):
    """The script a command runs, read the way ``swe hooks list`` reads it."""

    def test_real_command_shapes(self):
        from swe.hooks.identity import Resolver

        guard = self.foreign(".swe/hooks/command-guard/command_guard.py")
        spaced = self.foreign("Jane Doe/hooks/x.py")
        link = self.home / ".claude" / "hooks" / "linked.py"
        link.symlink_to(spaced)
        gone = self.home / "gone" / "old.py"
        r = Resolver(self.home)
        real = lambda p: os.path.realpath(p)
        shapes = {
            # A variable set once to a path, then run.
            'f="$HOME/.swe/hooks/command-guard/command_guard.py"; '
            '[ -f "$f" ] || exit 0; exec python3 "$f"': real(guard),
            f'python3 "{spaced}" --flag': real(spaced),
            f"bash -c 'python3 \"{spaced}\"'": real(spaced),
            # A link resolves to the file it points at.
            f'python3 "$HOME/.claude/hooks/linked.py"': real(spaced),
            # A file gone since still names the run.
            f"python3 {gone}": real(gone),
            # Nothing swe can know.
            "${CLAUDE_PLUGIN_ROOT}/hooks/on-stop.sh": None,
            "python3 hooks/relative.py": None,
            "notify-send done": None,
            # Two scripts in one command: no single identity.
            f'python3 "{guard}" && python3 "{spaced}"': None,
        }
        for command, want in shapes.items():
            self.assertEqual(r.script(command)[0], want, command)

    def test_a_wrapper_resolves_to_its_shared_script(self):
        from swe.hooks.identity import Resolver

        wrapper = self.shared("notify.py")
        script, wrapped = Resolver(self.home).script(f'python3 "{wrapper}"')
        self.assertTrue(wrapped)
        self.assertEqual(script, os.path.realpath(
            _shared.shared_dir(self.home) / "notify.py"))
        self.assertEqual(Resolver(self.home).name(f'python3 "{wrapper}"')[0],
                         "notify")


class WebMatchesCliTest(_IdentityHome):
    """The web page's rows join to the list exactly as the CLI's do."""

    def test_the_web_payload_carries_the_same_join(self):
        from swe.webapp.pages import hooks as page

        guard = self.hub("bundle/guard.py")
        cmd = self.declare(f'python3 "{guard}"')
        self.ran(cmd, HOUR)
        self.ran(cmd, 2 * HOUR, session="s2")
        with mock.patch.object(Path, "home", staticmethod(lambda: self.home)):
            web = page.build_stats_payload(self.home, "7d")
        cli = self.stats()
        pick = lambda d: [(r["source_paths"], r["fires"], r["top_reasons"])
                          for r in d["hooks"]]
        self.assertEqual(pick(web), pick(cli))
        self.assertEqual(web["uncertain"], cli["uncertain"])
        self.assertEqual(len(web["hooks"][0]["recent"]), 2)


if __name__ == "__main__":
    unittest.main()
