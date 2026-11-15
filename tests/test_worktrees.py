"""`swe wt`: discovery, classification, prune and slim (issue 1909).

Every repo lives in a temp folder. ``origin`` reads as a GitHub URL (so
the PR lookups run) but ``url.<path>.insteadOf`` sends git itself to a
local bare repo, and a fake ``gh`` on PATH answers PR queries from a JSON
file, so nothing touches the network.
"""

import io
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests._tempdir import temp_dir

from swe.worktrees import actions, discover, sizing
from swe.worktrees.classify import PrLookup, classify_all
from swe.worktrees import api
from swe.worktrees.api import WorktreeError
from swe.worktrees.models import (MAIN, MERGED_CLEAN, MERGED_DIRTY, NO_PR,
                                  OPEN_PR, ORPHANED, PROTECTED, UNKNOWN)
from swe.worktrees.settings import Settings

SLUG = "acme/widget"
URL = f"https://github.com/{SLUG}.git"

# The shebang is this interpreter, never ``/usr/bin/env python3``: the Nix
# build sandbox has no /usr/bin/env, so that stub failed to exec and every
# PR lookup came back as "gh is not installed".
FAKE_GH = "#!" + sys.executable + "\n" + textwrap.dedent("""\
    import json, os, sys
    if os.environ.get("FAKE_GH_FAIL"):
        sys.stderr.write("API rate limit exceeded\\n")
        sys.exit(1)
    data = json.load(open(os.environ["FAKE_GH_DATA"]))
    args = sys.argv[1:]
    if args[:2] == ["pr", "list"]:
        head = args[args.index("--head") + 1]
        print(json.dumps(data.get(head, [])))
    elif args[:2] == ["pr", "view"]:
        pr = data.get("#" + args[2])
        if pr is None:
            sys.stderr.write("no pull requests found\\n")
            sys.exit(1)
        print(json.dumps(pr))
    else:
        sys.exit(2)
""")


def run(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return done.stdout.strip()


def pr(number, state, head=None):
    return {"number": number, "state": state, "headRefOid": head,
            "url": f"https://github.com/{SLUG}/pull/{number}",
            "mergedAt": "2026-11-01T00:00:00Z" if state == "MERGED" else None}


class Fixture:
    """A repo with one worktree per case."""

    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        origin = base / "origin.git"
        run("init", "--bare", "-q", "-b", "main", str(origin))
        self.root = str((base / "widget").resolve())
        run("init", "-q", "-b", "main", self.root)
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                           ("commit.gpgsign", "false"),
                           ("remote.origin.url", URL),
                           ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                           (f"url.{origin}.insteadOf", URL)):
            run("config", key, value, cwd=self.root)
        Path(self.root, ".gitignore").write_text("node_modules/\n.venv/\n")
        Path(self.root, "README").write_text("hi\n")
        run("add", ".", cwd=self.root)
        run("commit", "-qm", "init", cwd=self.root)
        run("push", "-q", "origin", "main", cwd=self.root)
        run("fetch", "-q", "origin", cwd=self.root)
        self.trees = Path(self.root, ".claude", "worktrees")
        self.gh_data: dict = {}
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(0o755)
        self.data_file = base / "gh.json"

    def worktree(self, name, branch=None, *, commit=True, detach=False):
        path = str(self.trees / name)
        if detach:
            run("worktree", "add", "-q", "--detach", path, "origin/main", cwd=self.root)
        else:
            run("worktree", "add", "-q", "-b", branch, path, "main", cwd=self.root)
            if commit:
                Path(path, f"{name}.txt").write_text(name)
                run("add", ".", cwd=path)
                run("commit", "-qm", name, cwd=path)
        return os.path.realpath(path)

    def head(self, path):
        return run("rev-parse", "HEAD", cwd=path)

    def env(self, **extra):
        self.data_file.write_text(json.dumps(self.gh_data))
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_GH_DATA": str(self.data_file)}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def settings(self, **block):
        return Settings({"worktree": block} if block else {}, self.home)

    def scan(self, settings=None):
        discover.clear_cache()
        settings = settings or self.settings()
        found = discover.Discovery(home=self.home, cwd=str(self.base),
                                   settings=settings)
        found.scope_roots([self.root])
        worktrees = found.collect()
        classify_all(worktrees, PrLookup(), settings, self.home)
        return {w.path: w for w in worktrees}

    def registered(self):
        out = run("worktree", "list", "--porcelain", cwd=self.root)
        return {os.path.realpath(l[9:]) for l in out.splitlines()
                if l.startswith("worktree ")}


def make_old(path):
    """Push a worktree's last use two days back."""
    old = time.time() - 2 * 86400
    gitdir = Path(path, ".git").read_text().split("gitdir:")[1].strip()
    for p in (Path(gitdir) / "HEAD", Path(gitdir) / "index",
              Path(gitdir) / "logs" / "HEAD", Path(path)):
        if p.exists():
            os.utime(p, (old, old))


class WorktreeCases(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(temp_dir(self))
        fx = self.fx
        self.squash = fx.worktree("squash", "feat/squash")
        self.dirty = fx.worktree("dirty", "feat/dirty")
        Path(self.dirty, "README").write_text("changed\n")
        self.open = fx.worktree("open", "feat/open")
        self.none = fx.worktree("none", "feat/none")
        self.ignored = fx.worktree("ignored", "feat/ignored")
        Path(self.ignored, ".env.local").write_text("X=1\n")
        self.detached = fx.worktree("detached", detach=True)
        make_old(self.detached)
        self.fresh = fx.worktree("fresh", detach=True)
        ghost = fx.trees / "ghost"
        ghost.mkdir(parents=True)
        (ghost / ".git").write_text(f"gitdir: {fx.root}/.git/worktrees/ghost-gone\n")
        self.ghost = os.path.realpath(ghost)
        fx.gh_data = {
            "feat/squash": [pr(1, "MERGED", fx.head(self.squash))],
            "feat/dirty": [pr(2, "MERGED")],
            "feat/open": [pr(3, "OPEN")],
            "feat/ignored": [pr(4, "MERGED", fx.head(self.ignored))],
        }
        self.settings = fx.settings(repos={SLUG: {"ignore": [".env.local"]}})

    def test_states(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
        self.assertEqual(got[self.squash].state, MERGED_CLEAN)  # squash merge
        self.assertEqual(got[self.squash].pr["number"], 1)
        self.assertEqual(got[self.dirty].state, MERGED_DIRTY)
        self.assertEqual(got[self.dirty].changes, 1)
        self.assertEqual(got[self.open].state, OPEN_PR)
        self.assertEqual(got[self.none].state, NO_PR)
        self.assertEqual(got[self.ignored].state, MERGED_CLEAN)
        self.assertEqual(got[self.detached].state, MERGED_CLEAN)
        self.assertIsNone(got[self.detached].hold)
        self.assertEqual(got[self.fresh].state, MERGED_CLEAN)
        self.assertIn("24h", got[self.fresh].hold)
        self.assertEqual(got[self.ghost].state, ORPHANED)
        self.assertFalse(got[self.ghost].registered)
        self.assertEqual(got[self.fx.root].state, MAIN)
        # The ancestry check the issue warns about really does say "unmerged".
        rc = subprocess.run(["git", "merge-base", "--is-ancestor", "feat/squash",
                             "origin/main"], cwd=self.fx.root).returncode
        self.assertNotEqual(rc, 0)

    def test_untracked_counts_without_ignore_list(self):
        with self.fx.env():
            got = self.fx.scan(self.fx.settings())
        self.assertEqual(got[self.ignored].state, MERGED_DIRTY)
        self.assertEqual(got[self.ignored].untracked, 1)

    def test_gh_unavailable_is_unknown_and_never_pruned(self):
        with self.fx.env(FAKE_GH_FAIL="1"):
            got = self.fx.scan(self.settings)
            self.assertEqual(got[self.squash].state, UNKNOWN)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, home=self.fx.home)
        removed = {r["path"] for r in plan.acted}
        self.assertNotIn(self.squash, removed)
        self.assertEqual(removed, {self.detached})  # ancestry needs no gh
        self.assertTrue(os.path.isdir(self.squash))

    def test_dry_run_changes_nothing(self):
        before = self.fx.registered()
        branches = run("branch", "--format=%(refname:short)", cwd=self.fx.root)
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=False, delete_branches=True,
                                 home=self.fx.home)
        self.assertEqual({r["path"] for r in plan.acted},
                         {self.squash, self.ignored, self.detached})
        self.assertEqual(self.fx.registered(), before)
        self.assertEqual(run("branch", "--format=%(refname:short)", cwd=self.fx.root),
                         branches)
        self.assertTrue(all(os.path.isdir(p) for p in before))
        self.assertIsNone(plan.log)
        self.assertFalse((self.fx.home / ".swe" / "logs").exists())

    def test_yes_removes_only_merged_clean_and_keeps_branches(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, home=self.fx.home)
        gone = {self.squash, self.ignored, self.detached}
        self.assertEqual({r["path"] for r in plan.acted}, gone)
        for path in gone:
            self.assertFalse(os.path.exists(path))
        for path in (self.dirty, self.open, self.none, self.fresh, self.ghost):
            self.assertTrue(os.path.isdir(path), path)
        self.assertEqual(self.fx.registered() & gone, set())
        for branch in ("feat/squash", "feat/ignored"):
            run("rev-parse", "--verify", branch, cwd=self.fx.root)
        self.assertTrue(plan.log and Path(plan.log).is_file())
        logged = json.loads(Path(plan.log).read_text())
        self.assertEqual(len(logged["acted"]), 3)

    def test_protected_is_kept(self):
        settings = self.fx.settings(protect=[self.squash],
                                    repos={SLUG: {"ignore": [".env.local"]}})
        with self.fx.env():
            got = self.fx.scan(settings)
            self.assertEqual(got[self.squash].state, PROTECTED)
            plan = actions.prune(list(got.values()), settings=settings,
                                 apply=True, home=self.fx.home)
        self.assertNotIn(self.squash, {r["path"] for r in plan.acted})
        self.assertTrue(os.path.isdir(self.squash))

    def test_install_cache_is_protected_by_default(self):
        settings = Settings({}, self.fx.home)
        cache = os.path.realpath(self.fx.home / ".swe" / "cache" / "install" / "src")
        self.assertIn(cache, settings.protect())

    def test_branch_filter_and_delete_branches(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, branch="feat/squash",
                                 delete_branches=True, home=self.fx.home)
        self.assertEqual([r["path"] for r in plan.acted], [self.squash])
        self.assertTrue(os.path.isdir(self.ignored))
        self.assertEqual([b["branch"] for b in plan.branches], ["feat/squash"])
        rc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "feat/squash"],
                            cwd=self.fx.root).returncode
        self.assertNotEqual(rc, 0)
        run("rev-parse", "--verify", "feat/ignored", cwd=self.fx.root)
        # Remote branches are never touched.
        self.assertIn("main", run("ls-remote", "--heads", "origin", cwd=self.fx.root))

    def test_merged_dirty_needs_flag_and_a_yes(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, include_dirty=True,
                                 confirm=lambda wt: False, home=self.fx.home)
            self.assertTrue(os.path.isdir(self.dirty))
            self.assertIn("not confirmed", " ".join(r["reason"] for r in plan.skipped))
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, include_dirty=True,
                                 confirm=lambda wt: True, home=self.fx.home)
        self.assertIn(self.dirty, {r["path"] for r in plan.acted})
        self.assertFalse(os.path.exists(self.dirty))

    def test_state_is_read_again_before_removal(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            Path(self.squash, "late.txt").write_text("new work\n")
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, home=self.fx.home)
        self.assertTrue(os.path.isdir(self.squash))
        reasons = {r["path"]: r["reason"] for r in plan.skipped}
        self.assertIn("state changed to merged-dirty", reasons[self.squash])

    def test_auto_prune_follows_repo_config(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, auto=True, home=self.fx.home)
            self.assertEqual(plan.acted, [])
            on = self.fx.settings(repos={SLUG: {"ignore": [".env.local"],
                                                "auto_prune": True}})
            got = self.fx.scan(on)
            plan = actions.prune(list(got.values()), settings=on, apply=True,
                                 auto=True, branch="feat/squash", home=self.fx.home)
        self.assertEqual([r["path"] for r in plan.acted], [self.squash])

    def test_slim_deletes_only_stale_ignored_dirs_in_unmerged(self):
        for path in (self.none, self.squash):
            mods = Path(path, "node_modules", "pkg")
            mods.mkdir(parents=True)
            (mods / "index.js").write_text("x")
            old = time.time() - 30 * 86400
            for p in (mods / "index.js", mods, mods.parent):
                os.utime(p, (old, old))
        Path(self.open, "build").mkdir()  # not ignored: never a candidate
        with self.fx.env():
            got = self.fx.scan(self.settings)
            sizing.measure_all(list(got.values()))
            dry = actions.slim(list(got.values()), apply=False, days=14)
            self.assertEqual([r["folder"] for r in dry.acted],
                             [os.path.join(self.none, "node_modules")])
            self.assertTrue(os.path.isdir(os.path.join(self.none, "node_modules")))
            done = actions.slim(list(got.values()), apply=True, days=14,
                                home=self.fx.home)
        self.assertEqual(len(done.acted), 1)
        self.assertFalse(os.path.exists(os.path.join(self.none, "node_modules")))
        self.assertTrue(os.path.isdir(self.none))
        self.assertTrue(os.path.isdir(os.path.join(self.squash, "node_modules")))
        self.assertTrue(os.path.isdir(os.path.join(self.open, "build")))

    def cli(self, *args, settings=None):
        from swe.worktrees import commands
        out = io.StringIO()
        with self.fx.env(), redirect_stdout(out), \
                mock.patch.object(api.Settings, "load",
                                  return_value=settings or self.settings), \
                mock.patch("os.getcwd", return_value=str(self.fx.base)):
            rc = commands.cmd_worktree([*args, "--json"])
        return rc, json.loads(out.getvalue())

    W_KEYS = {"path", "repo", "main_checkout", "branch", "head", "state", "pr",
              "last_commit_at", "size_bytes", "rebuildable_bytes", "dirty",
              "untracked", "locked", "lock_reason", "harness"}

    def test_list_json_contract(self):
        rc, doc = self.cli("list", f"--repo={self.fx.root}")
        self.assertEqual(rc, 0)
        self.assertEqual(doc["schema"], 1)
        data = doc["data"]
        self.assertEqual(data["scope"], "repo")
        self.assertIn("generated_at", data)
        rows = {w["path"]: w for w in data["worktrees"]}
        row = rows[self.squash]
        self.assertLessEqual(self.W_KEYS, set(row))
        self.assertEqual(row["repo"], SLUG)
        self.assertEqual(row["main_checkout"], self.fx.root)
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["pr"], {"number": 1, "state": "MERGED",
                                     "url": f"https://github.com/{SLUG}/pull/1"})
        self.assertIsInstance(row["size_bytes"], int)
        self.assertFalse(row["dirty"])
        self.assertTrue(rows[self.dirty]["dirty"])
        self.assertEqual(rows[self.fx.root]["state"], MAIN)
        self.assertEqual(data["totals"]["by_state"][MERGED_CLEAN], 4)
        self.assertEqual(data["totals"]["prunable"], 3)

    def test_prune_cli_dry_run_by_default(self):
        rc, doc = self.cli("prune", f"--repo={self.fx.root}")
        self.assertEqual(rc, 0)
        data = doc["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual({w["path"] for w in data["candidates"]},
                         {self.squash, self.ignored, self.detached})
        self.assertEqual(data["removed"], [])
        self.assertIsInstance(data["freed_bytes"], int)
        self.assertTrue(all(set(r) >= {"path", "reason"} for r in data["skipped"]))
        self.assertTrue(os.path.isdir(self.squash))

    def test_prune_cli_yes(self):
        rc, doc = self.cli("prune", f"--repo={self.fx.root}", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(set(doc["data"]["removed"]),
                         {self.squash, self.ignored, self.detached})
        self.assertFalse(os.path.exists(self.squash))

    def test_show(self):
        rc, doc = self.cli("show", self.open)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["data"]["worktree"]["state"], OPEN_PR)
        rc, doc = self.cli("show", os.path.join(self.open, "missing"))
        self.assertEqual(rc, 1)
        self.assertEqual(doc["error"]["code"], "not_found")

    def test_remove_rules(self):
        rc, doc = self.cli("remove", self.fx.root)
        self.assertEqual((rc, doc["error"]["code"]), (1, "not_safe"))
        rc, doc = self.cli("remove", self.open)
        self.assertEqual((rc, doc["error"]["code"]), (1, "not_safe"))
        self.assertTrue(os.path.isdir(self.open))
        rc, doc = self.cli("remove", self.ghost, "--force")
        self.assertEqual(doc["error"]["code"], "not_safe")
        self.assertTrue(os.path.isdir(self.ghost))
        rc, doc = self.cli("remove", self.squash, "--delete-branch")
        self.assertEqual(rc, 0)
        self.assertEqual(doc["data"]["removed"], self.squash)
        self.assertIn("freed_bytes", doc["data"])
        self.assertTrue(doc["data"]["branch_deleted"])
        self.assertFalse(os.path.exists(self.squash))
        rc, doc = self.cli("remove", self.open, "--force", "--delete-branch")
        self.assertEqual(rc, 0)
        self.assertFalse(doc["data"]["branch_deleted"])  # unmerged: -d refuses
        run("rev-parse", "--verify", "feat/open", cwd=self.fx.root)

    def test_protected_cannot_be_removed_even_forced(self):
        settings = self.fx.settings(protect=[self.none])
        rc, doc = self.cli("remove", self.none, "--force", settings=settings)
        self.assertEqual(doc["error"]["code"], "not_safe")
        self.assertTrue(os.path.isdir(self.none))

    def test_lock_unlock_move(self):
        rc, doc = self.cli("lock", self.none, "--reason=keep me")
        self.assertEqual(rc, 0)
        w = doc["data"]["worktree"]
        self.assertTrue(w["locked"])
        self.assertEqual(w["lock_reason"], "keep me")
        rc, doc = self.cli("move", self.none, str(self.fx.base / "elsewhere"))
        self.assertEqual(doc["error"]["code"], "not_safe")
        rc, doc = self.cli("unlock", self.none)
        self.assertFalse(doc["data"]["worktree"]["locked"])
        target = os.path.realpath(self.fx.base) + "/moved/none"
        rc, doc = self.cli("move", self.none, target)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["data"]["worktree"]["path"], target)
        self.assertTrue(os.path.isdir(target))
        self.assertFalse(os.path.exists(self.none))
        rc, doc = self.cli("move", self.fx.root, str(self.fx.base / "x"))
        self.assertEqual(doc["error"]["code"], "not_safe")

    def test_add_branch_new_and_pr(self):
        rc, doc = self.cli("add", self.fx.root, "--branch=feat/fresh", "--new")
        self.assertEqual(rc, 0, doc)
        w = doc["data"]["worktree"]
        expected = os.path.join(os.path.dirname(self.fx.root), "widget-worktrees",
                                "feat-fresh")
        self.assertEqual(w["path"], expected)
        self.assertEqual(w["branch"], "feat/fresh")
        rc, doc = self.cli("add", self.fx.root, "--branch=feat/fresh", "--new")
        self.assertEqual(doc["error"]["code"], "exists")
        rc, doc = self.cli("add", self.fx.root, "--branch=nope")
        self.assertEqual(doc["error"]["code"], "not_found")
        rc, doc = self.cli("add", self.fx.root)
        self.assertEqual((rc, doc["error"]["code"]), (2, "bad_args"))
        # A PR head, fetched from refs/pull/N/head on origin.
        origin = str(self.fx.base / "origin.git")
        sha = self.fx.head(self.open)
        run("push", "-q", origin, f"{sha}:refs/pull/9/head", cwd=self.fx.root)
        self.fx.gh_data["#9"] = {**pr(9, "OPEN", sha), "headRefName": "feat/from-pr"}
        rc, doc = self.cli("add", self.fx.root, "--pr=9",
                           f"--path={self.fx.base}/pr9")
        self.assertEqual(rc, 0, doc)
        w = doc["data"]["worktree"]
        self.assertEqual(w["branch"], "feat/from-pr")
        self.assertEqual(w["head"], sha)
        # A PR whose head name is taken falls back to pr-N and keeps its link.
        run("push", "-q", origin, f"{sha}:refs/pull/10/head", cwd=self.fx.root)
        self.fx.gh_data["#10"] = {**pr(10, "OPEN", sha), "headRefName": "main"}
        rc, doc = self.cli("add", self.fx.root, "--pr=10")
        self.assertEqual(rc, 0, doc)
        w = doc["data"]["worktree"]
        self.assertEqual(w["branch"], "pr-10")
        self.assertEqual((w["state"], w["pr"]["number"]), (OPEN_PR, 10))
        # Another PR whose head is named pr-10 never overrides the saved link.
        self.fx.gh_data["pr-10"] = [pr(99, "MERGED", sha)]
        rc, doc = self.cli("show", w["path"])
        self.assertEqual(doc["data"]["worktree"]["pr"]["number"], 10)
        self.assertEqual(doc["data"]["worktree"]["state"], OPEN_PR)
        # Once PR 10 merges, more commits on pr-10 keep the checkout.
        self.fx.gh_data.pop("pr-10")
        self.fx.gh_data["#10"] = {**pr(10, "MERGED", sha), "headRefName": "main"}
        rc, doc = self.cli("show", w["path"])
        self.assertEqual(doc["data"]["worktree"]["state"], MERGED_CLEAN)
        Path(w["path"], "more.txt").write_text("x")
        run("add", ".", cwd=w["path"])
        run("commit", "-qm", "more", cwd=w["path"])
        rc, doc = self.cli("show", w["path"])
        self.assertEqual(doc["data"]["worktree"]["state"], NO_PR)
        self.fx.gh_data["pr-10"] = [pr(11, "OPEN")]
        rc, doc = self.cli("show", w["path"])
        self.assertEqual(doc["data"]["worktree"]["state"], OPEN_PR)

    def test_slim_explicit_path(self):
        mods = Path(self.squash, "node_modules")
        mods.mkdir()
        (mods / "a.js").write_text("x")
        rc, doc = self.cli("slim", self.squash)
        self.assertEqual(rc, 0)
        self.assertTrue(doc["data"]["dry_run"])
        self.assertEqual([d["path"] for d in doc["data"]["dirs"]], [str(mods)])
        self.assertTrue(mods.is_dir())
        rc, doc = self.cli("slim", self.squash, "--yes")
        self.assertFalse(mods.exists())
        self.assertTrue(os.path.isdir(self.squash))
        rc, doc = self.cli("slim", self.fx.root)
        self.assertEqual(doc["error"]["code"], "not_safe")

    def test_python_api_raises(self):
        with self.assertRaises(WorktreeError) as caught:
            api.remove(str(self.fx.base / "nowhere"))
        self.assertEqual(caught.exception.code, "not_found")
        import swe.worktrees as pkg
        for name in ("list_worktrees", "show", "add", "remove", "lock",
                     "unlock", "move", "prune", "slim", "WorktreeError"):
            self.assertTrue(hasattr(pkg, name), name)

    def test_squash_branch_with_later_commits_keeps_its_branch(self):
        Path(self.squash, "later.txt").write_text("after merge\n")
        run("add", ".", cwd=self.squash)
        run("commit", "-qm", "after merge", cwd=self.squash)
        rc, doc = self.cli("remove", self.squash, "--delete-branch")
        self.assertEqual(rc, 0, doc)
        self.assertFalse(doc["data"]["branch_deleted"])  # tip is past the PR head
        run("rev-parse", "--verify", "feat/squash", cwd=self.fx.root)

    def test_branch_switch_after_scan_is_caught(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            run("checkout", "-q", "-b", "feat/next", cwd=self.squash)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, home=self.fx.home)
        self.assertTrue(os.path.isdir(self.squash))
        reasons = {r["path"]: r["reason"] for r in plan.skipped}
        self.assertIn("moved since the scan", reasons[self.squash])

    def test_auto_skips_repo_wide_steps_when_off(self):
        with self.fx.env():
            got = self.fx.scan(self.settings)
            plan = actions.prune(list(got.values()), settings=self.settings,
                                 apply=True, auto=True, delete_branches=True,
                                 home=self.fx.home)
        self.assertEqual((plan.acted, plan.branches), ([], []))

    def test_bad_flags(self):
        rc, doc = self.cli("prune", f"--repo={self.fx.root}", "--include=open-pr")
        self.assertEqual((rc, doc["error"]["code"]), (2, "bad_args"))
        self.assertEqual(self.cli("prune", "--branch")[0], 2)
        self.assertEqual(self.cli("list", "--yes")[0], 2)
        self.assertEqual(self.cli("nope")[0], 2)
        self.assertEqual(self.cli("move", self.none)[0], 2)


class HarnessFolders(unittest.TestCase):
    def test_codex_layout_and_pr_checkout_name(self):
        fx = Fixture(temp_dir(self))
        accounts = fx.home / ".swe" / "accounts" / "codex" / "work" / "home" / "worktrees"
        (accounts / "task").mkdir(parents=True)
        path = os.path.realpath(accounts / "task" / "widget")
        run("worktree", "add", "-q", "-b", "codex/task", path, "main", cwd=fx.root)
        prs = Path(fx.home / ".swe" / "worktrees")
        prs.mkdir(parents=True)
        pr_path = os.path.realpath(prs / "acme__widget-pr7")
        run("worktree", "add", "-q", "--detach", pr_path, "main", cwd=fx.root)
        fx.gh_data = {"#7": pr(7, "MERGED")}
        settings = fx.settings()
        with fx.env():
            discover.clear_cache()
            found = discover.Discovery(home=fx.home, cwd=str(fx.base), settings=settings)
            found.scope_everywhere()
            got = {w.path: w for w in found.collect()}
            classify_all(list(got.values()), PrLookup(), settings, fx.home)
        self.assertEqual(got[path].location, "codex")
        self.assertEqual(got[pr_path].location, "swe")
        self.assertEqual(got[pr_path].state, MERGED_CLEAN)
        self.assertEqual(got[pr_path].pr["number"], 7)
        # A commit past the merged PR's head lives only in that checkout.
        Path(pr_path, "extra.txt").write_text("x")
        run("add", ".", cwd=pr_path)
        run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "extra", cwd=pr_path)
        fx.gh_data = {"#7": pr(7, "MERGED", run("rev-parse", "main", cwd=fx.root))}
        with fx.env():
            discover.clear_cache()
            found = discover.Discovery(home=fx.home, cwd=str(fx.base), settings=settings)
            found.scope_everywhere()
            again = {w.path: w for w in found.collect()}
            classify_all(list(again.values()), PrLookup(), settings, fx.home)
        self.assertEqual(again[pr_path].state, NO_PR)


class Parsing(unittest.TestCase):
    def test_porcelain_z(self):
        rows = discover.parse_porcelain(
            "worktree /r\0HEAD a\0branch refs/heads/main\0\0"
            "worktree /odd\tname\0HEAD b\0detached\0locked why\0\0")
        self.assertEqual(rows[1]["path"], "/odd\tname")
        self.assertEqual(rows[1]["lock_reason"], "why")

    def test_porcelain(self):
        rows = discover.parse_porcelain(
            "worktree /r\nHEAD a\nbranch refs/heads/main\n\n"
            "worktree /r/w\nHEAD b\ndetached\nlocked reason\n\n"
            "worktree /gone\nHEAD c\nbranch refs/heads/x\nprunable gitdir file points to non-existent location\n")
        self.assertEqual(rows[0]["branch"], "main")
        self.assertTrue(rows[1]["detached"] and rows[1]["locked"])
        self.assertTrue(rows[2]["prunable"])

    def test_github_slug(self):
        self.assertEqual(discover.github_slug("git@github.com:a/b.git"), "a/b")
        self.assertEqual(discover.github_slug("https://github.com/a/b"), "a/b")
        self.assertIsNone(discover.github_slug("https://gitlab.com/a/b"))

    def test_config_validation(self):
        from swe.configuration import validate_config
        issues = validate_config({"worktree": {"roots": "x", "repos": {
            "a/b": {"ignore": [1], "auto_prune": "yes"}}}})
        paths = {i.path for i in issues}
        self.assertEqual(paths, {"worktree.roots", "worktree.repos.a/b.ignore",
                                 "worktree.repos.a/b.auto_prune"})
        self.assertEqual(validate_config({"worktree": {"roots": ["~/code"]}}), [])

    def test_human(self):
        self.assertEqual(sizing.human(None), "-")
        self.assertEqual(sizing.human(2 * 1024 * 1024), "2.0 GB")



class HumanOutput(unittest.TestCase):
    """The terminal rendering of every subcommand, over the same fixture."""

    def setUp(self):
        self.fx = Fixture(temp_dir(self))
        fx = self.fx
        self.squash = fx.worktree("squash", "feat/squash")
        self.dirty = fx.worktree("dirty", "feat/dirty")
        Path(self.dirty, "README").write_text("changed\n")
        self.none = fx.worktree("none", "feat/none")
        mods = Path(self.none, "node_modules")
        mods.mkdir()
        (mods / "a.js").write_text("x")
        old = time.time() - 30 * 86400
        for p in (mods / "a.js", mods):
            os.utime(p, (old, old))
        fx.gh_data = {"feat/squash": [pr(1, "MERGED", fx.head(self.squash))],
                      "feat/dirty": [pr(2, "MERGED")]}
        # A root folder holding the repo, so --repo walks one level down.
        self.settings = fx.settings(roots=[str(fx.base)],
                                    repos={fx.root: {"ignore": []}})

    def run_cli(self, *args, isatty=False):
        from swe.worktrees import commands
        out = io.StringIO()
        with self.fx.env(), redirect_stdout(out), \
                mock.patch.object(api.Settings, "load", return_value=self.settings), \
                mock.patch("os.getcwd", return_value=self.fx.root), \
                mock.patch.object(sys.stdin, "isatty", return_value=isatty), \
                mock.patch.object(sys.stderr, "isatty", return_value=True), \
                mock.patch("swe.console.terminal_width", return_value=200):
            rc = commands.cmd_worktree(list(args))
        return rc, out.getvalue()

    def test_list_and_show(self):
        rc, text = self.run_cli("list")
        self.assertEqual(rc, 0)
        for word in ("(main checkout)", "merged-clean", "merged-dirty", "#1",
                     "swe wt prune --yes", "du estimates"):
            self.assertIn(word, text)
        rc, text = self.run_cli("ls", "--no-size", f"--repo={self.fx.base}")
        self.assertEqual(rc, 0)
        self.assertNotIn("du estimates", text)
        rc, text = self.run_cli("show", self.squash)
        self.assertIn("PR #1 merged", text)
        self.assertIn("harness", text)

    def test_all_scope_and_help(self):
        rc, text = self.run_cli("list", "--all", "--no-size")
        self.assertEqual(rc, 0)
        self.assertIn("feat/squash", text)
        rc, text = self.run_cli("help")
        self.assertEqual(rc, 0)
        self.assertIn("swe wt prune", text)
        rc, text = self.run_cli("list", "--repo=/nonexistent/place")
        self.assertEqual(rc, 1)

    def test_single_worktree_commands(self):
        rc, text = self.run_cli("add", self.fx.root, "--branch=feat/x", "--new")
        self.assertEqual(rc, 0, text)
        self.assertIn("Created", text)
        made = os.path.join(os.path.dirname(self.fx.root), "widget-worktrees", "feat-x")
        rc, text = self.run_cli("lock", made, "--reason=busy")
        self.assertIn("Locked", text)
        rc, text = self.run_cli("list", "--no-size")
        self.assertIn("locked: busy", text)
        rc, text = self.run_cli("unlock", made)
        self.assertIn("Unlocked", text)
        moved = made + "-moved"
        rc, text = self.run_cli("mv", made, moved)
        self.assertIn("Moved to", text)
        rc, text = self.run_cli("rm", moved, "--force", "--delete-branch")
        self.assertEqual(rc, 0, text)
        self.assertIn("Removed", text)
        rc, text = self.run_cli("remove", self.squash, "--delete-branch")
        self.assertIn("Deleted its local branch", text)
        rc, text = self.run_cli("remove", self.dirty)
        self.assertEqual(rc, 1)
        self.assertIn("--force", text)

    def test_prune_and_slim_output(self):
        rc, text = self.run_cli("prune", "--delete-branches")
        self.assertEqual(rc, 0)
        self.assertIn("would remove", text)
        self.assertIn("Dry run", text)
        self.assertIn("pass --include=merged-dirty", text)
        rc, text = self.run_cli("prune", "--include=merged-dirty", "--yes")
        self.assertIn("Removed 1 worktree(s)", text)
        self.assertIn("not confirmed", text)  # no terminal, no yes
        self.assertIn("Log:", text)
        self.assertTrue(os.path.isdir(self.dirty))
        with mock.patch("swe.prompt.read_line", return_value="y"):
            rc, text = self.run_cli("prune", "--include=merged-dirty", "--yes",
                                    isatty=True)
        self.assertFalse(os.path.exists(self.dirty))
        rc, text = self.run_cli("slim")
        self.assertIn("would delete", text)
        rc, text = self.run_cli("slim", "--yes", "--days=7")
        self.assertIn("Deleted 1 folder(s)", text)
        self.assertFalse(os.path.exists(os.path.join(self.none, "node_modules")))
        rc, text = self.run_cli("slim", self.none, "--all")
        self.assertEqual(rc, 2)
        rc, text = self.run_cli("slim", "--days=soon")
        self.assertEqual(rc, 2)

    def test_lock_is_exclusive(self):
        with actions.RunLock(self.fx.home) as held:
            self.assertTrue(held.held)
            with self.assertRaises(actions.Busy):
                with actions.RunLock(self.fx.home):
                    pass
        self.assertFalse(held.held)
        with actions.RunLock(self.fx.home) as again:  # released on exit
            self.assertTrue(again.held)

    def test_space_form_is_refused(self):
        rc, text = self.run_cli("lock", self.none, "--reason", "busy")
        self.assertEqual(rc, 2)
        self.assertIn("--reason=<value>", text)
        rc, _ = self.run_cli("slim", "--days=\u00b2")
        self.assertEqual(rc, 2)

    def test_broken_config_refuses(self):
        cfg = self.fx.home / ".swe" / "config" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{not json")
        with self.fx.env(), mock.patch("os.getcwd", return_value=self.fx.root):
            with self.assertRaises(WorktreeError) as caught:
                api.prune(home=self.fx.home, repo=self.fx.root)
        self.assertEqual(caught.exception.code, "bad_config")
        with self.fx.env():
            with self.assertRaises(WorktreeError):
                api.add(self.fx.root, branch="feat/later", new=True, home=self.fx.home)
        self.assertFalse(os.path.exists(os.path.join(
            os.path.dirname(self.fx.root), "widget-worktrees", "feat-later")))

    def test_settings_by_path_key(self):
        settings = Settings({"worktree": {"repos": {
            "~/x": {"auto_prune": True}}}}, self.fx.home)
        repo = discover.find_repo(self.fx.root)
        self.assertFalse(settings.auto_prune(repo))
        home_repo = discover.Repo(root=os.path.realpath(self.fx.home / "x"),
                                  common_dir="/x/.git")
        self.assertTrue(settings.auto_prune(home_repo))
        self.assertEqual(Settings({"worktree": "bad"}).roots(), [])


if __name__ == "__main__":
    unittest.main()
