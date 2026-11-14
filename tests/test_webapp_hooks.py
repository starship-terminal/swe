"""The Hooks page: the ~/.swe/hooks inventory with its link states and
declarations, the check findings, swe init run for it, and swe hooks new."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from swe.webapp.pages import PageError
from swe.webapp.pages import hooks as page

from tests.test_remote_hub import HubBase
from tests.test_webapp_doctor import _TempHome


def _hook(home: Path, harness: str, name: str, body: str = "print('hi')\n") -> Path:
    """One script under ~/.swe/hooks/<harness>/, as test_hooks.py writes it."""
    path = home / ".swe" / "hooks" / harness / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _settings(home: Path, rel: str, hooks: dict) -> Path:
    """One settings file in the grouped {Event: [{matcher, hooks}]} shape."""
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}))
    return path


def _link(home: Path, harness: str, name: str) -> Path:
    """The destination link a finished swe init leaves for one script."""
    dest = home / ".claude" / "hooks" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(home / ".swe" / "hooks" / harness / name)
    return dest


def _decl(command: str, event="PreToolUse", matcher="*", timeout=10) -> dict:
    return {event: [{"matcher": matcher,
                     "hooks": [{"type": "command", "command": command, "timeout": timeout}]}]}


class _HooksHome(_TempHome):
    """A temp home with claude registered and installed (~/.claude)."""

    def setUp(self):
        super().setUp()
        self.seed({"claude": {"command": "claude"}})
        (self.home / ".claude").mkdir()

    def hooks(self, payload):
        return {h["name"]: h for h in payload["hooks"]}


class TestList(_HooksHome, unittest.TestCase):
    def test_payload_shape_and_row_keys(self):
        _hook(self.home, "claude", "guard.py")
        d = page.get(self.ctx, {})
        self.assertEqual((d["machine"], d["home"]), ("hub-box", str(self.home)))
        self.assertEqual((d["warning"], d["check_error"], d["blocked"]), (None, None, []))
        self.assertIn("claude", d["harnesses"])
        row = self.hooks(d)["guard.py"]
        self.assertEqual(
            {k: row[k] for k in ("name", "harness", "tier", "link_status", "enabled", "declared", "detail")},
            {"name": "guard.py", "harness": "claude", "tier": "harness",
             "link_status": "create", "enabled": True, "declared": [], "detail": ""})
        self.assertEqual(row["source_path"], str(self.home / ".swe/hooks/claude/guard.py"))
        self.assertEqual(row["dest_path"], str(self.home / ".claude/hooks/guard.py"))
        self.assertIn("PreToolUse", row["events_supported"])

    def test_tiers_shared_harness_and_foreign(self):
        _hook(self.home, "claude", "guard.py")
        _hook(self.home, "shared", "notify.py")
        foreign = self.home / ".claude" / "hooks" / "other.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("print('theirs')\n")
        rows = self.hooks(page.get(self.ctx, {}))
        self.assertEqual((rows["guard.py"]["tier"], rows["guard.py"]["harness"]), ("harness", "claude"))
        self.assertEqual((rows["notify.py"]["tier"], rows["notify.py"]["harness"]), ("shared", "shared"))
        self.assertEqual((rows["other.py"]["tier"], rows["other.py"]["link_status"], rows["other.py"]["enabled"]),
                         ("foreign", "foreign", True))
        # Inventory order: harness rows, shared, then foreign.
        self.assertEqual([h["tier"] for h in page.get(self.ctx, {})["hooks"]],
                         ["harness", "shared", "foreign"])

    def test_a_declaration_joins_its_row(self):
        _hook(self.home, "claude", "guard.py")
        settings = _settings(self.home, ".claude/settings.json",
                             _decl('python3 "~/.claude/hooks/guard.py"'))
        row = self.hooks(page.get(self.ctx, {}))["guard.py"]
        self.assertEqual(row["declared"], [
            {"event": "PreToolUse", "matcher": "*", "file": str(settings)}])

    def test_findings_cover_each_kind(self):
        _hook(self.home, "claude", "guard.py")          # create -> unlinked
        _hook(self.home, "claude", "quiet.py")
        _link(self.home, "claude", "quiet.py")          # linked, undeclared
        _settings(self.home, ".claude/settings.json",   # ghost -> declared-missing
                  _decl('python3 "~/.claude/hooks/ghost.py"'))
        managed = self.home / ".claude" / "settings.local.json"
        _settings(self.home, ".claude/real-local.json", {})
        os.symlink(self.home / ".claude" / "real-local.json", managed)
        kinds = {f["kind"] for f in page.get(self.ctx, {})["findings"]}
        self.assertEqual(kinds, {"declared-missing", "undeclared", "unlinked", "managed"})
        managed_finding = next(f for f in page.get(self.ctx, {})["findings"] if f["kind"] == "managed")
        self.assertEqual((managed_finding["harness"], "managed" in managed_finding["message"]),
                         ("claude", True))

    def test_a_check_that_fails_degrades_not_dies(self):
        _hook(self.home, "claude", "guard.py")
        with patch("swe.hooks.inventory.hook_findings", side_effect=ValueError("boom")):
            d = page.get(self.ctx, {})
        self.assertEqual(d["findings"], [])
        self.assertIn("boom", d["check_error"])
        self.assertEqual([h["name"] for h in d["hooks"]], ["guard.py"])

    def test_an_unreadable_linkignore_warns_not_dies(self):
        _hook(self.home, "claude", "guard.py")
        (self.home / ".swe" / ".linkignore").mkdir()
        d = page.get(self.ctx, {})
        self.assertIn("linkignore", d["warning"])
        self.assertEqual([h["name"] for h in d["hooks"]], ["guard.py"])

    def test_empty_home_lists_nothing(self):
        d = page.get(self.ctx, {})
        self.assertEqual((d["hooks"], d["findings"]), ([], []))


class TestDetail(_HooksHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.seed({"claude": {"command": "claude", "aliases": ["cc"]},
                   "codex": {"command": "codex"}})
        (self.home / ".codex").mkdir()
        _hook(self.home, "claude", "guard.py", body='"""Stop bad prose."""\nimport sys\n')
        _hook(self.home, "codex", "guard.py")
        self.foreign = self.home / ".claude" / "hooks" / "other.sh"
        self.foreign.parent.mkdir(parents=True, exist_ok=True)
        self.foreign.write_text("#!/bin/sh\n# Foreign, no hub source.\n")

    def test_detail_reads_the_script(self):
        d = page.get(self.ctx, {"hook": "guard.py"})
        self.assertEqual((d["harness"], d["tier"], d["editable"], d["is_dir"], d["exists"]),
                         ("claude", "harness", True, False, True))
        self.assertEqual(d["summary"], "Stop bad prose.")
        self.assertIn("import sys", d["text"])

    def test_a_stem_finds_the_script(self):
        d = page.get(self.ctx, {"hook": "guard", "harness": "claude"})
        self.assertEqual(d["name"], "guard.py")

    def test_harness_scopes_an_ambiguous_name(self):
        d = page.get(self.ctx, {"hook": "guard.py", "harness": "codex"})
        self.assertEqual((d["harness"], d["source_path"]),
                         ("codex", str(self.home / ".swe/hooks/codex/guard.py")))
        d = page.get(self.ctx, {"hook": "guard.py", "harness": "cc"})
        self.assertEqual(d["harness"], "claude")

    def test_tier_picks_between_same_name_and_harness(self):
        # The shared tier's guard.py deploys to claude, so two rows answer
        # to (guard.py, claude): only the tier tells them apart.
        _hook(self.home, "claude", "guard.py", "print('harness')\n")
        _hook(self.home, "shared", "guard.py", "# events: [PreToolUse]\nprint('shared')\n")
        shared = page.get(self.ctx, {"hook": "guard.py", "harness": "claude", "tier": "shared"})
        self.assertEqual((shared["tier"], shared["source_path"]),
                         ("shared", str(self.home / ".swe/hooks/shared/guard.py")))
        own = page.get(self.ctx, {"hook": "guard.py", "harness": "claude", "tier": "harness"})
        self.assertEqual((own["tier"], own["source_path"]),
                         ("harness", str(self.home / ".swe/hooks/claude/guard.py")))

    def test_full_declarations_with_command_and_timeout(self):
        settings = _settings(self.home, ".claude/settings.json",
                             _decl('python3 "~/.claude/hooks/guard.py"', timeout=30))
        d = page.get(self.ctx, {"hook": "guard.py", "harness": "claude"})
        self.assertEqual(d["declared"], [{
            "event": "PreToolUse", "matcher": "*", "timeout": 30,
            "command": 'python3 "~/.claude/hooks/guard.py"', "file": str(settings)}])

    def test_a_foreign_script_is_not_editable(self):
        d = page.get(self.ctx, {"hook": "other.sh", "harness": "claude"})
        self.assertEqual((d["tier"], d["editable"], d["exists"]), ("foreign", False, True))
        self.assertIn("#!/bin/sh", d["text"])

    def test_a_foreign_script_outside_home_is_not_read(self):
        # A hooks entry can resolve anywhere on disk; its text must not
        # reach a paired device unless it lives under the hub's home.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        outside = Path(folder.name) / "secret.sh"
        outside.write_text("#!/bin/sh\n# TOKEN=abc in the header\nTOKEN=abc\n")
        (self.home / ".claude" / "hooks" / "ext.sh").symlink_to(outside)
        d = page.get(self.ctx, {"hook": "ext.sh", "harness": "claude"})
        self.assertEqual((d["tier"], d["exists"]), ("foreign", True))
        self.assertIsNone(d["text"])
        self.assertEqual(d["summary"], "")
        self.assertNotIn("abc", json.dumps(d))

    def test_a_script_that_vanished_reads_as_missing(self):
        # The inventory only ever lists files that exist, so a missing
        # source is a race: the row the list built, then the file went.
        _hook(self.home, "claude", "ghost.py")
        stale = page._inventory(self.home)
        (self.home / ".swe" / "hooks" / "claude" / "ghost.py").unlink()
        with patch.object(page, "_inventory", return_value=stale):
            d = page.get(self.ctx, {"hook": "ghost.py"})
        self.assertFalse(d["exists"])
        self.assertIsNone(d["text"])
        self.assertFalse(d["editable"])

    def test_unknown_hook_and_unknown_scoped_hook_are_404(self):
        with self.assertRaises(PageError) as caught:
            page.get(self.ctx, {"hook": "nope.py"})
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "unknown_hook"))
        with self.assertRaises(PageError) as caught:
            page.get(self.ctx, {"hook": "guard.py", "harness": "freebuff"})
        self.assertEqual(caught.exception.status, 404)


class TestLink(_HooksHome, unittest.TestCase):
    """Link with swe init and Replace and back up, run for real."""

    def test_link_links_the_script_and_relists(self):
        _hook(self.home, "claude", "guard.py")
        self.assertEqual(self.hooks(page.get(self.ctx, {}))["guard.py"]["link_status"], "create")
        reply = page.post(self.ctx, "link", {})
        self.assertEqual((reply["ran"], reply["error"]), ("link", None))
        self.assertEqual(self.hooks(reply)["guard.py"]["link_status"], "linked")
        dest = self.home / ".claude" / "hooks" / "guard.py"
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), (self.home / ".swe/hooks/claude/guard.py").resolve())
        kinds = {f["kind"] for f in reply["findings"]}
        self.assertIn("undeclared", kinds)   # linked but nothing invokes it

    def test_blocked_lists_the_real_file(self):
        _hook(self.home, "claude", "guard.py")
        dest = self.home / ".claude" / "hooks"
        dest.mkdir(parents=True)
        (dest / "guard.py").write_text("different bytes\n")
        d = page.get(self.ctx, {})
        self.assertEqual(self.hooks(d)["guard.py"]["link_status"], "conflict")
        self.assertIn("~/.claude/hooks/guard.py", d["blocked"])
        reply = page.post(self.ctx, "link", {})
        self.assertEqual((reply["ran"], reply["error"]), ("link", None))
        self.assertIn("~/.claude/hooks/guard.py", reply["blocked"])
        self.assertEqual((dest / "guard.py").read_text(), "different bytes\n")

    def test_replace_backs_up_and_links(self):
        _hook(self.home, "claude", "guard.py")
        dest = self.home / ".claude" / "hooks"
        dest.mkdir(parents=True)
        (dest / "guard.py").write_text("different bytes\n")
        reply = page.post(self.ctx, "replace", {})
        self.assertEqual((reply["ran"], reply["error"]), ("replace", None))
        self.assertEqual(self.hooks(reply)["guard.py"]["link_status"], "linked")
        self.assertTrue((dest / "guard.py").is_symlink())
        backups = list((self.home / ".swe" / "backups").iterdir())
        self.assertEqual([b.read_text() for b in backups], ["different bytes\n"])
        self.assertIn("guard.py", backups[0].name)

    def test_link_runs_plain_init_and_replace_forces(self):
        calls = []
        with patch("swe.webapp.pages.doctor._run_init",
                   side_effect=lambda home, force=False: calls.append(force)):
            page.post(self.ctx, "link", {})
            page.post(self.ctx, "replace", {})
        self.assertEqual(calls, [False, True])

    def test_unknown_actions_are_404(self):
        for action in ("init", "sync", "delete", ""):
            with self.assertRaises(PageError) as caught:
                page.post(self.ctx, action, {})
            self.assertEqual((caught.exception.status, caught.exception.code), (404, "unknown_action"))

    def test_one_fix_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def slow(home, force=False):
            started.set()
            release.wait(5)

        with patch("swe.webapp.pages.doctor._run_init", side_effect=slow):
            first = threading.Thread(target=page.post, args=(self.ctx, "link", {}))
            first.start()
            started.wait(5)
            try:
                for action in ("link", "replace"):
                    with self.assertRaises(PageError) as caught:
                        page.post(self.ctx, action, {})
                    self.assertEqual((caught.exception.status, caught.exception.code),
                                     (409, "busy"), action)
            finally:
                release.set()
                first.join(5)


class TestNew(_HooksHome, unittest.TestCase):
    def test_new_writes_the_skeleton_and_the_stanza_to_paste(self):
        reply = page.post(self.ctx, "new", {"harness": "claude", "name": "sentinel"})
        self.assertEqual(reply["action"], "new")
        created = reply["created"]
        target = self.home / ".swe" / "hooks" / "claude" / "sentinel.py"
        self.assertEqual(created["path"], str(target))
        self.assertEqual(created["harness"], "claude")
        self.assertTrue(target.is_file())
        self.assertTrue(os.access(target, os.X_OK))
        self.assertIn("sentinel.py", created["command"])
        self.assertTrue(created["settings"])
        stanza = json.loads(created["stanza"])
        command = stanza["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(command, created["command"])
        # swe never writes a settings file: nothing was created for it.
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse((self.home / ".claude" / "settings.local.json").exists())
        # The reply is the list read again: the new row is there.
        self.assertIn("sentinel.py", self.hooks(reply))

    def test_new_shared_writes_the_shared_tier(self):
        reply = page.post(self.ctx, "new", {"harness": "shared", "name": "notify"})
        created = reply["created"]
        self.assertEqual((created["tier"], created["harness"]), ("shared", "shared"))
        self.assertEqual(created["path"], str(self.home / ".swe" / "hooks" / "shared" / "notify.py"))
        self.assertTrue(Path(created["path"]).is_file())
        # The skeleton declares [PreToolUse]; installed shim-able claude gets a wrapper command.
        self.assertIn("claude", created["deploys_to"])
        self.assertIn("_generated/notify.py", created["commands"]["claude"])
        self.assertIsNotNone(created["stanza"])

    def test_bad_names_are_422(self):
        for body in ({"harness": "claude", "name": "has space"},
                     {"harness": "claude", "name": "-lead"},
                     {"harness": "claude", "name": "../escape"},
                     {"harness": "claude"}, {"name": "x"},
                     {"harness": 1, "name": "x"}):
            with self.assertRaises(PageError) as caught:
                page.post(self.ctx, "new", body)
            self.assertEqual((caught.exception.status, caught.exception.code), (422, "bad_hook"), body)

    def test_an_existing_name_is_422_and_kept(self):
        existing = _hook(self.home, "claude", "guard.py", body="mine\n")
        with self.assertRaises(PageError) as caught:
            page.post(self.ctx, "new", {"harness": "claude", "name": "guard"})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(existing.read_text(), "mine\n")

    def test_a_harness_alias_resolves(self):
        self.seed({"claude": {"command": "claude", "aliases": ["cc"]}})
        reply = page.post(self.ctx, "new", {"harness": "cc", "name": "via_alias"})
        self.assertTrue((self.home / ".swe" / "hooks" / "claude" / "via_alias.py").is_file())
        self.assertEqual(reply["created"]["harness"], "claude")


class TestRoute(HubBase):
    def setUp(self):
        super().setUp()
        self.ctx = SimpleNamespace(home=self.srv.home, machine=self.srv.state.machine)
        # The hub's own home is a real home for the page: ~/.swe exists.
        (self.srv.home / ".swe").mkdir(exist_ok=True)
        _hook(self.srv.home, "claude", "guard.py")
        (self.srv.home / ".claude").mkdir()

    def origin(self):
        return {"Origin": f"http://127.0.0.1:{self.srv.port}"}

    def test_get_needs_a_device(self):
        self.assertEqual(self.srv.request("GET", "/v1/app/hooks")[0], 401)
        status, data = self.srv.request("GET", "/v1/app/hooks", headers=self.auth())
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertEqual(body["machine"], self.srv.state.machine["name"])
        self.assertEqual([h["name"] for h in body["hooks"]], ["guard.py"])

    def test_post_needs_a_device_and_our_origin(self):
        self.assertEqual(
            self.srv.request("POST", "/v1/app/hooks/link", body={})[0], 401)
        with patch("swe.webapp.pages.doctor._run_init", return_value=None) as ran:
            status, data = self.srv.request(
                "POST", "/v1/app/hooks/replace", body={},
                headers={**self.auth(), "Origin": "https://evil.example"})
            self.assertEqual(status, 403)
            self.assertIn("bad_origin", data.decode())
            ran.assert_not_called()
            status, data = self.srv.request(
                "POST", "/v1/app/hooks/link", body={}, headers={**self.auth(), **self.origin()})
            self.assertEqual(status, 200, data)
            self.assertEqual(json.loads(data)["ran"], "link")
            self.assertEqual(ran.call_args.kwargs, {"force": False})

    def test_detail_and_errors_carry_their_status(self):
        auth = self.auth()
        status, data = self.srv.request(
            "GET", "/v1/app/hooks?hook=" + quote("guard.py"), headers=auth)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["name"], "guard.py")
        status, data = self.srv.request(
            "GET", "/v1/app/hooks?hook=" + quote("nope.py"), headers=auth)
        self.assertEqual(status, 404)
        error = json.loads(data)["error"]
        self.assertEqual((error["code"], bool(error["message"])), ("unknown_hook", True))
        status, data = self.srv.request(
            "POST", "/v1/app/hooks/new", body={"harness": "claude", "name": "bad name"},
            headers={**auth, **self.origin()})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_hook")

    def test_page_assets_are_bundled(self):
        status, js = self.srv.request("GET", "/pages.js")
        self.assertEqual(status, 200)
        _, css = self.srv.request("GET", "/pages.css")
        self.assertIn(b".hk-row", css)


class TestFindingMessage(unittest.TestCase):
    """hkFindingMessage says each fact once: the badge reads "Managed
    elsewhere", so the note keeps only the path, and home paths read ~/."""

    def test_managed_note_is_its_path_and_home_reads_tilde(self):
        import re
        import shutil
        import subprocess

        if not shutil.which("node"):
            self.skipTest("node is not installed")
        js = (Path(page.__file__).resolve().parents[1] / "static/pages/hooks.js").read_text(encoding="utf-8")
        fn = re.search(r"^function hkFindingMessage\(.*?^\}\n", js, re.S | re.M).group(0)
        script = fn + """
process.stdout.write(JSON.stringify([
  hkFindingMessage({kind: "managed", message: "/Users/k/.claude/settings.json — declaration managed elsewhere"}, "/Users/k"),
  hkFindingMessage({kind: "unlinked", message: "g.py is not linked into /Users/k/.claude/hooks (swe init will link it)"}, "/Users/k/"),
  hkFindingMessage({kind: "undeclared", message: "x at /Users/kb/y"}, "/Users/k"),
  hkFindingMessage({kind: "managed", message: "/backup/Users/k/s.json — declaration managed elsewhere"}, "/Users/k"),
]));
"""
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [
            "~/.claude/settings.json",
            "g.py is not linked into ~/.claude/hooks (swe init will link it)",
            "x at /Users/kb/y",
            "/backup/Users/k/s.json",
        ])


# -- run stats ---------------------------------------------------------------

HOUR, DAY = 3600, 86400


def _event(now, ago, hook, decision, harness="claude", **extra) -> dict:
    """One line of ~/.swe/cache/hooks/events.jsonl (docs/data-model.md)."""
    row = {"v": 1, "ts": now - ago, "hook": hook, "harness": harness,
           "event": "Stop", "session_id": "s1", "cwd": "/w/app",
           "repo": "/w/app", "git_branch": "main", "decision": decision,
           "reason": "", "duration_ms": 10, "exit_code": 0,
           "stop_hook_active": False}
    row.update(extra)
    return row


def _write_events(home: Path, rows: list) -> None:
    from swe import paths

    path = paths.hook_events_file_for(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class _StatsHome(unittest.TestCase):
    """A throwaway home, an empty stats cache and no transcripts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.now = __import__("time").time()
        page._STATS.clear()
        self.addCleanup(page._STATS.clear)
        for target in (patch("pathlib.Path.home", return_value=self.home),
                       patch("swe.hooks.transcripts.backfill", return_value=[])):
            target.start()
            self.addCleanup(target.stop)

    def payload(self, window="7d"):
        return page.build_stats_payload(self.home, window, self.now)

    def row(self, payload, hook):
        return {r["hook"]: r for r in payload["hooks"]}[hook]


class TestStatsNumbers(_StatsHome):
    def test_a_hook_is_summed_across_harnesses(self):
        _write_events(self.home, [
            _event(self.now, HOUR, "prose-guard", "block", reason="no em dash"),
            _event(self.now, 2 * HOUR, "prose-guard", "allow"),
            _event(self.now, 3 * HOUR, "prose-guard", "allow", harness="codex"),
            _event(self.now, 4 * HOUR, "command_guard", "allow"),
        ])
        out = self.payload()
        row = self.row(out, "prose-guard")
        self.assertEqual((row["fires"], row["blocks"], row["block_rate"]), (3, 1, 0.3333))
        self.assertEqual(row["harnesses"], ["claude", "codex"])
        self.assertNotIn("harness", row)
        self.assertEqual({r["hook"] for r in out["hooks"]}, {"prose-guard", "command_guard"})
        self.assertEqual((out["state"], out["window"], out["totals"]["fires"]), ("ready", "7d", 4))

    def test_zero_blocks_and_no_runs(self):
        _write_events(self.home, [_event(self.now, HOUR, "pr-behind-main", "allow")])
        out = self.payload()
        row = self.row(out, "pr-behind-main")
        self.assertEqual((row["fires"], row["blocks"], row["block_rate"], row["last_block"]),
                         (1, 0, 0.0, None))
        # A hook with no runs has no row at all: the page shows dashes.
        self.assertNotIn("no-sleep-poll", {r["hook"] for r in out["hooks"]})
        empty = page.build_stats_payload(self.home / "nobody", "7d", self.now)
        self.assertEqual((empty["hooks"], empty["totals"]["fires"]), ([], 0))

    def test_the_window_picks_which_runs_count(self):
        _write_events(self.home, [
            _event(self.now, 2 * HOUR, "g", "block", reason="a"),
            _event(self.now, 3 * DAY, "g", "block", reason="b"),
            _event(self.now, 20 * DAY, "g", "block", reason="c"),
            _event(self.now, 40 * DAY, "g", "block", reason="d"),
        ])
        fires = {w: self.row(self.payload(w), "g")["fires"] for w in ("24h", "7d", "30d")}
        self.assertEqual(fires, {"24h": 1, "7d": 2, "30d": 3})

    def test_detail_reasons_and_recent_runs(self):
        rows = [_event(self.now, i * HOUR, "g", "allow", git_branch=f"b{i}") for i in range(1, 8)]
        rows += [_event(self.now, 9 * HOUR, "g", "block", reason="no em dash", repo="/w/api"),
                 _event(self.now, 10 * HOUR, "g", "block", reason="no em dash"),
                 _event(self.now, 11 * HOUR, "g", "warn", reason="long reply")]
        _write_events(self.home, rows)
        row = self.row(self.payload(), "g")
        self.assertEqual(row["top_reasons"], [
            {"decision": "block", "reason": "no em dash", "count": 2},
            {"decision": "warn", "reason": "long reply", "count": 1}])
        # Five newest runs, newest first, as `swe hooks log --json` shows them.
        self.assertEqual([r["git_branch"] for r in row["recent"]], ["b1", "b2", "b3", "b4", "b5"])
        self.assertEqual(set(row["recent"][0]) >= {"ts", "decision", "repo", "cwd", "git_branch", "harness"}, True)
        self.assertEqual(row["last_block"], self.now - 9 * HOUR)

    def test_transcript_runs_join_the_log(self):
        _write_events(self.home, [_event(self.now, HOUR, "g", "block", reason="x", session_id="a")])
        transcript = dict(_event(self.now, 2 * HOUR, "g", "block", reason="y", session_id="b"),
                          source="transcript")
        with patch("swe.hooks.transcripts.backfill", return_value=[transcript]):
            out = self.payload()
        self.assertEqual(self.row(out, "g")["fires"], 2)
        self.assertEqual((out["sources"]["helper"], out["sources"]["transcript"]), (1, 1))

    def test_the_numbers_match_the_cli(self):
        """The page reads what `swe hooks stats --json` prints."""
        from swe.hooks import events as hook_events
        from swe.hooks import stats_commands

        _write_events(self.home, [
            _event(self.now, HOUR, "g", "block", reason="a"),
            _event(self.now, 2 * HOUR, "g", "allow", harness="codex")])
        data = stats_commands.collect(self.home, self.now - 7 * DAY, True, self.now)
        cli = hook_events.build_stats(data["events"])["totals"]
        out = self.payload()
        self.assertEqual({k: out["totals"][k] for k in ("fires", "blocks", "block_rate")},
                         {k: cli[k] for k in ("fires", "blocks", "block_rate")})


class TestStatsRead(_StatsHome):
    """The read runs off the request and is cached."""

    def setUp(self):
        super().setUp()
        _write_events(self.home, [_event(self.now, HOUR, "g", "block", reason="a")])
        patcher = patch.object(page, "FIRST_WAIT_S", 5.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_view_answers_ready_and_is_cached(self):
        real = page.build_stats_payload
        with patch.object(page, "build_stats_payload", side_effect=real) as built:
            first = page.get(SimpleNamespace(home=self.home), {"stats": "7d"})
            second = page.get(SimpleNamespace(home=self.home), {"stats": "7d"})
        self.assertEqual((first["state"], first["hooks"][0]["hook"]), ("ready", "g"))
        self.assertIs(first, second)
        self.assertEqual(built.call_count, 1)

    def test_a_bad_window_is_422(self):
        for bad in ("1y", "all", "7d;", "../x"):
            with self.assertRaises(PageError) as ctx:
                page.get(SimpleNamespace(home=self.home), {"stats": bad})
            self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "bad_window"))

    def test_a_slow_read_never_blocks_the_list_or_the_request(self):
        gate, started = threading.Event(), threading.Event()
        real = page.build_stats_payload
        calls = []

        def slow(home, window, now=None):
            calls.append(window)
            started.set()
            self.assertTrue(gate.wait(10))
            return real(home, window, now)

        ctx = SimpleNamespace(home=self.home, machine={"name": "hub-box"})
        with patch.object(page, "FIRST_WAIT_S", 0.05), patch.object(page, "build_stats_payload", side_effect=slow):
            self.assertEqual(page.get(ctx, {"stats": "7d"}), {"state": "loading", "window": "7d"})
            self.assertTrue(started.wait(5))
            # The list reads while the stats read is still stuck.
            self.assertEqual(page.get(ctx, {})["machine"], "hub-box")
            # A second ask joins the read in flight: it does not start another.
            self.assertEqual(page.get(ctx, {"stats": "7d"})["state"], "loading")
            self.assertEqual(calls, ["7d"])
            # Another window is its own read, also off the request.
            self.assertEqual(page.get(ctx, {"stats": "24h"})["state"], "loading")
            threads = [e["thread"] for e in page._STATS.values() if "thread" in e]
            gate.set()
            for thread in threads:
                thread.join(10)
            self.assertEqual(page.get(ctx, {"stats": "7d"})["state"], "ready")
            self.assertEqual(sorted(calls), ["24h", "7d"])

    def test_a_failed_read_is_a_503_and_retried_after_its_ttl(self):
        real = page.build_stats_payload
        with patch.object(page, "build_stats_payload", side_effect=OSError("disk gone")):
            with self.assertRaises(PageError) as ctx:
                page.get(SimpleNamespace(home=self.home), {"stats": "7d"})
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "stats_unavailable"))
        self.assertIn("disk gone", ctx.exception.message)
        # Within the error TTL the failure stands; past it the read runs again.
        with patch.object(page, "STATS_ERROR_TTL_S", 0.0), \
                patch.object(page, "build_stats_payload", side_effect=real):
            self.assertEqual(page.get(SimpleNamespace(home=self.home), {"stats": "7d"})["state"], "ready")

    def test_an_old_answer_is_served_while_the_next_read_runs(self):
        ctx = SimpleNamespace(home=self.home)
        first = page.get(ctx, {"stats": "7d"})
        gate = threading.Event()
        real = page.build_stats_payload

        def slow(home, window, now=None):
            gate.wait(10)
            return real(home, window, now)

        with patch.object(page, "STATS_TTL_S", 0.0), patch.object(page, "build_stats_payload", side_effect=slow):
            self.assertIs(page.get(ctx, {"stats": "7d"}), first)
            thread = page._STATS[(str(self.home), "7d")]["thread"]
            gate.set()
            thread.join(10)


class TestStatsRoute(HubBase):
    def setUp(self):
        super().setUp()
        page._STATS.clear()
        self.addCleanup(page._STATS.clear)
        self.srv.home.joinpath(".swe").mkdir(exist_ok=True)
        _write_events(self.srv.home, [
            _event(__import__("time").time(), HOUR, "guard", "block", reason="no em dash"),
            _event(__import__("time").time(), 2 * HOUR, "guard", "allow", harness="codex")])
        for target in (patch.object(page, "FIRST_WAIT_S", 10.0),
                       patch("swe.hooks.transcripts.backfill", return_value=[])):
            target.start()
            self.addCleanup(target.stop)

    def test_stats_need_a_device(self):
        self.assertEqual(self.srv.request("GET", "/v1/app/hooks?stats=7d")[0], 401)

    def test_stats_for_a_window(self):
        status, data = self.srv.request("GET", "/v1/app/hooks?stats=7d", headers=self.auth())
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertEqual(body["state"], "ready")
        self.assertEqual([(h["hook"], h["fires"], h["blocks"]) for h in body["hooks"]], [("guard", 2, 1)])

    def test_a_bad_window_is_422(self):
        status, data = self.srv.request("GET", "/v1/app/hooks?stats=9y", headers=self.auth())
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_window")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestStatsMarkup(unittest.TestCase):
    """hooks.js's number columns and detail card, run under node with the
    page's own helpers stubbed."""

    STUBS = """
const registerPage = () => {};
const Sk = { num: (w) => `<i class="skel"></i>`, bar: () => `<i class="skel"></i>` };
const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const Fmt = { ago: (t) => `ago${t}` };
let stored = null;
const localStorage = { getItem: () => { if (stored === "throw") throw new Error("off"); return stored; },
                       setItem: (k, v) => { stored = v; } };
"""

    def run_js(self, body: str):
        js = (Path(page.__file__).resolve().parents[1] / "static/pages/hooks.js").read_text(encoding="utf-8")
        done = subprocess.run(["node", "-e", self.STUBS + js + "\n" + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    ROWS = """
const row = (o) => Object.assign({ hook: "g", fires: 12, blocks: 3, warns: 1, errors: 0, block_rate: 0.25,
  last_block: 1000, top_reasons: [], recent: [] }, o);
const ready = (rows) => ({ state: "ready", rows, at: Date.now() });
"""

    def test_a_script_name_loses_its_extension(self):
        out = self.run_js('process.stdout.write(JSON.stringify(["prose-guard.py", "command_guard.sh", "/h/x/no-sleep-poll.py", "plain", ".py", "a.b.py"].map(hkStem)));')
        self.assertEqual(out, ["prose-guard", "command_guard", "no-sleep-poll", "plain", ".py", "a.b"])

    def test_the_three_columns_per_state(self):
        out = self.run_js(self.ROWS + """
const cells = (html) => (html.match(/<span class="hk-num[^"]*">.*?<\\/span>/g) || []).map((c) => c.replace(/<[^>]+>/g, "") + "|" + (c.match(/hk-(bad|zero)/) || [""])[0]);
hooksPage.window = "7d";
const w = hkWindow();
const out = {};
hooksPage.statsBy[w] = ready({ g: row({}), calm: row({ hook: "calm", fires: 5, blocks: 0, block_rate: 0 }),
  tiny: row({ hook: "tiny", fires: 400, blocks: 1, block_rate: 0.0025 }), idle: row({ hook: "idle", fires: 0, blocks: 0 }) });
out.busy = cells(hkNumsHtml("g.py"));
out.calm = cells(hkNumsHtml("calm.sh"));
out.tiny = cells(hkNumsHtml("tiny.py"));
out.idle = cells(hkNumsHtml("idle.py"));
out.none = cells(hkNumsHtml("never-ran.py"));
hooksPage.statsBy[w] = { state: "error", rows: {}, at: 0 };
out.error = cells(hkNumsHtml("g.py"));
hooksPage.statsBy[w] = { state: "loading", rows: {}, at: 0 };
out.loading = hkNumsHtml("g.py");
out.header = hkColsHeader().replace(/<[^>]+>/g, " ").trim().split(/\\s+/);
process.stdout.write(JSON.stringify(out));
""")
        self.assertEqual(out["busy"], ["12|", "3|hk-bad", "25%|hk-bad"])
        self.assertEqual(out["calm"], ["5|", "0|hk-zero", "0%|hk-zero"])
        self.assertEqual(out["tiny"], ["400|", "1|hk-bad", "&lt;1%|hk-bad"])
        dashes = ["–|hk-zero"] * 3
        self.assertEqual((out["idle"], out["none"], out["error"]), (dashes, dashes, dashes))
        # Loading keeps three cells too, each holding a placeholder.
        self.assertEqual(out["loading"].count('class="hk-num"'), 3)
        self.assertEqual(out["loading"].count("skel"), 3)
        self.assertEqual(out["header"], ["Fires", "Blocks", "Rate"])

    def test_the_picker_offers_three_windows_and_remembers_one(self):
        out = self.run_js("""
const out = {};
out.first = hkWindow();
out.options = [...hkWindowSelect().matchAll(/<option value="(\\w+)"( selected)?>([^<]*)/g)].map((m) => [m[1], !!m[2], m[3]]);
hooksPage.window = null; stored = "30d"; out.saved = hkWindow();
hooksPage.window = null; stored = "bogus"; out.bogus = hkWindow();
hooksPage.window = null; stored = "throw"; out.blocked = hkWindow();
process.stdout.write(JSON.stringify(out));
""")
        self.assertEqual(out["first"], "7d")
        self.assertEqual(out["options"], [["24h", False, "Last 24 hours"], ["7d", True, "Last 7 days"],
                                          ["30d", False, "Last 30 days"]])
        self.assertEqual((out["saved"], out["bogus"], out["blocked"]), ("30d", "7d", "7d"))

    def test_the_detail_card(self):
        out = self.run_js(self.ROWS + """
const text = (html) => html.replace(/<[^>]+>/g, "|").replace(/\\s*\\|\\s*/g, "|").replace(/\\|+/g, "|");
const out = {};
hooksPage.window = "7d";
const runs = Array.from({ length: 7 }, (_, i) => ({ ts: 2000 - i, decision: i ? "allow" : "block", repo: "/w/api", cwd: "/w/api", git_branch: `b${i}` }));
hooksPage.statsBy["7d"] = ready({ g: row({ top_reasons: [
  { decision: "block", reason: "no em dash", count: 2 }, { decision: "warn", reason: "long reply", count: 1 },
  { decision: "error", reason: "boom", count: 4 }], recent: runs }) });
out.full = text(hkDetailStatsInner("g.py"));
out.recentRows = (hkDetailStatsInner("g.py").match(/hk-line-row/g) || []).length;
out.blocksBad = /hk-tile-value hk-bad">3</.test(hkDetailStatsInner("g.py"));
out.noRuns = text(hkDetailStatsInner("other.py"));
hooksPage.window = "24h"; hooksPage.statsBy["24h"] = ready({});
out.noRuns24 = text(hkDetailStatsInner("g.py"));
hooksPage.statsBy["24h"] = ready({ g: row({ blocks: 0, last_block: null, block_rate: 0 }) });
out.never = text(hkDetailStatsInner("g.py"));
hooksPage.statsBy["24h"] = { state: "error", rows: {}, at: 0 };
out.error = text(hkDetailStatsInner("g.py"));
process.stdout.write(JSON.stringify(out));
""")
        full = out["full"]
        for want in ("Fires|12", "Blocks|3", "Last block|ago1000", "Top reasons", "no em dash|2 blocks",
                     "long reply|1 warn", "boom|4 errors", "Recent runs", "block|· api · b0|ago2000",
                     "allow|· api · b4|ago1996"):
            self.assertIn(want, full)
        self.assertNotIn("b5", full)       # five runs at most
        self.assertEqual(out["recentRows"], 3 + 5)
        self.assertTrue(out["blocksBad"])
        self.assertIn("No runs in the last 7 days.", out["noRuns"])
        self.assertNotIn("Top reasons", out["noRuns"])
        self.assertIn("No runs in the last 24 hours.", out["noRuns24"])
        self.assertIn("Last block|–", out["never"])
        self.assertIn("not available", out["error"])

    def test_phone_width_keeps_the_columns_and_truncates_the_name(self):
        css = (Path(page.__file__).resolve().parents[1] / "static/pages/hooks.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.hk-row \.hk-title \.name \{[^}]*text-overflow: ellipsis")
        self.assertRegex(css, r"\.hk-num \{[^}]*width: 46px")
        self.assertRegex(css, r"@media \(max-width: 480px\) \{ \.hk-num \{ width: 40px; \} \}")
        self.assertRegex(css, r"@media \(prefers-reduced-motion: reduce\) \{ \.hk-fresh \{ animation: none; \} \}")
        # Numbers never wrap or collapse: the name gives way, not them.
        self.assertRegex(css, r"\.hk-nums \{[^}]*flex: none")


if __name__ == "__main__":
    unittest.main()
