"""The Skills page: the standalone skills, one skill's detail, and the
guard that keeps file reads inside the skill's folder."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from swe.webapp.pages import PageError
from swe.webapp.pages import skills as page

from tests.test_remote_hub import HubBase
from tests.test_webapp import app_js_function

from tests._tempdir import temp_dir


def _skill(name, path, source="harness", harnesses=("codex",), root="codex"):
    return {"name": name, "description": f"{name} does things", "path": str(path),
            "root": root, "source": source, "harnesses": list(harnesses)}


def _catalog(plugin_folder):
    return {"marketplaces": [{"plugins": [{
        "ref": "tools@market", "source_path": str(plugin_folder),
        "harnesses": {"claude": {"path": None, "copies": []}},
    }]}]}


class TestFrontmatter(unittest.TestCase):
    def test_simple_fields_only(self):
        text = "---\nname: tdd\ndescription:  Use it\nmetadata:\n  tags: [a]\nbad key: x\nlicense: MIT\n---\nbody\n"
        self.assertEqual(page.frontmatter(text), [
            {"key": "name", "value": "tdd"}, {"key": "description", "value": "Use it"},
            {"key": "license", "value": "MIT"}])

    def test_block_values_read_as_yaml_reads_them(self):
        # The same cases as FrontmatterBlockTests in the Swift suite; the
        # values come from Ruby's Psych (libyaml).
        cases = [
            ("description: |\n  Line one.\n  Line two.\nname: x\n", "Line one.\nLine two.\n"),
            ("description: >\n  Fold these\n  into one.\n\n  New para.\nname: x\n", "Fold these into one.\nNew para.\n"),
            ("description: |-\n  a\n  b\n\nname: x\n", "a\nb"),
            ("description: |+\n  a\n  b\n\n\nname: x\n", "a\nb\n\n\n"),
            ("description: >+\n  a\n\n", "a\n\n"),
            ("description: >\n  a\n  b\n    code\n  c\n", "a b\n  code\nc\n"),
            ("description: |2\n    lead\n  b\n", "  lead\nb\n"),
            ("description: >-4\n      x\n    y\n", "  x\ny"),
            ("description: | # note\n  text\n", "text\n"),
            ("description: |\nname: x\n", ""),
            ("description: |\n  a\n    \n  b\n", "a\n  \nb\n"),
            ("description: |\n\n  a\n", "\na\n"),
            ("description: |\n  a\n  \tb\n", "a\n\tb\n"),
            ("description: >\n  a\n\n\n  b\n", "a\n\nb\n"),
            ("description: >\n  a\n   \n  b\n", "a\n \nb\n"),
            ("description: Use this when\n  the user asks for\n\n  a review.\nname: x\n",
             "Use this when the user asks for\na review."),
            ("description:\n  starts below\n  and wraps\n", "starts below and wraps"),
        ]
        for inside, value in cases:
            with self.subTest(inside=inside):
                fields = page.frontmatter("---\n" + inside + "---\n\n# Body\n")
                self.assertEqual(next(f["value"] for f in fields if f["key"] == "description"), value)

    def test_nested_values_and_bad_blocks_are_left_out(self):
        inside = ("name: x\ndescription: |\n  Words.\nmetadata:\n  author: me\ntriggers:\n  - one\n"
                  "list: a\n  - b\nquoted: \"a\n  b\"\nbad: |\n    deep\n  shallow\n")
        self.assertEqual(page.frontmatter("---\n" + inside + "---\n"), [
            {"key": "name", "value": "x"}, {"key": "description", "value": "Words.\n"}])

    def test_a_last_line_of_spaces_is_the_blocks(self):
        # A line of only spaces deeper than the block's indentation is its
        # content; the scan that ignores indentation used to call it blank
        # and drop the field (review on #1985).
        fields = page.frontmatter("---\ndescription: |-\n  a\n   \nname: x\n---\n")
        self.assertEqual(fields, [{"key": "description", "value": "a\n "}, {"key": "name", "value": "x"}])

    def test_crlf_lines_read_like_lf(self):
        text = "---\r\nname: x\r\ndescription: |\r\n  one\r\n  two\r\n---\r\nbody\r\n"
        self.assertEqual(page.frontmatter(text), [
            {"key": "name", "value": "x"}, {"key": "description", "value": "one\ntwo\n"}])

    def test_no_frontmatter(self):
        self.assertIsNone(page.frontmatter("# Title\n"))
        self.assertIsNone(page.frontmatter("---\nname: open\n"))
        self.assertEqual(page.frontmatter("---\n---\n"), [])


class TestReadText(unittest.TestCase):
    def setUp(self):
        self.dir = temp_dir(self)

    def test_text_binary_and_big(self):
        (self.dir / "a.md").write_text("héllo")
        (self.dir / "b.bin").write_bytes(b"\x00\x01\x02")
        (self.dir / "c.md").write_bytes(b"x" * (page.MAX_TEXT + 1))
        self.assertEqual(page.read_text(self.dir / "a.md"), "héllo")
        self.assertIsNone(page.read_text(self.dir / "b.bin"))
        self.assertIsNone(page.read_text(self.dir / "c.md"))
        self.assertIsNone(page.read_text(self.dir / "missing.md"))
        self.assertIsNone(page.read_text(self.dir))
        (self.dir / "d.txt").write_bytes(b"ok \xff\xfe")
        self.assertIsNone(page.read_text(self.dir / "d.txt"))
        (self.dir / "e.md").write_bytes(b"a" * 8191 + "é and more".encode())
        self.assertTrue(page.read_text(self.dir / "e.md").endswith("é and more"))


class TestPlacement(unittest.TestCase):
    def test_plugin_skills_owned_by_one_plugin_move(self):
        base = temp_dir(self)
        plugin = base / "tools"
        (plugin / "skills" / "lint").mkdir(parents=True)
        own = _skill("lint", plugin / "skills" / "lint", source="plugin", harnesses=["claude"])
        loose = _skill("stray", base / "elsewhere", source="plugin", harnesses=["claude"])
        mine = _skill("tdd", base / "mine")
        with patch("swe.plugins.catalog.build_catalog", return_value=_catalog(plugin)):
            placed, moved, error = page.placement([own, loose, mine], base)
        self.assertEqual([s["plugin"] for s in placed], ["tools@market", None, None])
        self.assertEqual((moved, error), (1, None))

    def test_placement_reuses_the_plugins_pages_catalog(self):
        # build_catalog walks every marketplace: one read serves both
        # pages until it expires or a plugin change drops it.
        from swe.webapp.pages import plugins
        base = temp_dir(self)
        self.addCleanup(plugins.clear_cache)
        skills = [_skill("lint", base / "lint", source="plugin")]
        with patch("swe.plugins.catalog.build_catalog",
                   return_value={"marketplaces": []}) as build:
            page.placement(skills, base)
            page.placement(skills, base)
            self.assertEqual(build.call_count, 1)
            plugins._invalidate(base)
            page.placement(skills, base)
            self.assertEqual(build.call_count, 2)

    def test_unreadable_catalog_keeps_every_skill(self):
        skills = [_skill("lint", "/x/lint", source="plugin")]
        with patch("swe.plugins.catalog.build_catalog", side_effect=ValueError("bad json")):
            placed, moved, error = page.placement(skills, Path("/x"))
        self.assertEqual((placed, moved, error), ([{**skills[0], "plugin": None}], 0, "bad json"))


class TestDetail(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.root = self.home / ".codex" / "skills" / "tdd"
        (self.root / "references").mkdir(parents=True)
        (self.root / "SKILL.md").write_text("---\nname: tdd\ndescription: Test first\n---\n# TDD <b>\n")
        (self.root / "references" / "loop.md").write_text("red, green")
        (self.root / ".hidden").write_text("x")
        (self.root / "logo.png").write_bytes(b"\x89PNG\x00\x00")
        secret = self.home / "secret.txt"
        secret.write_text("private")
        os.symlink(secret, self.root / "escape.md")
        self.skill = _skill("tdd", self.root)

    def test_detail_reads_frontmatter_skill_md_and_tree(self):
        d = page.detail(self.skill, self.home)
        self.assertFalse(d["editable"])
        self.assertEqual(d["skill_md"].splitlines()[-1], "# TDD <b>")
        self.assertEqual(d["frontmatter"][1], {"key": "description", "value": "Test first"})
        # SKILL.md is the skill's main file, not one of its other files.
        self.assertEqual([s["kind"] for s in d["tree"]], ["main", "other"])
        self.assertEqual(d["tree"][0], {"kind": "main", "title": "Main file", "entries": [
            {"name": "SKILL.md", "folder": False, "path": "SKILL.md"}]})
        names = [e["name"] for e in d["tree"][1]["entries"]]
        self.assertEqual(names, ["escape.md", "logo.png", "references/loop.md"])

    def test_sections_for_plugin_shaped_folders(self):
        (self.root / "skills" / "inner").mkdir(parents=True)
        (self.root / "skills" / "inner" / "SKILL.md").write_text("x")
        (self.root / "commands").mkdir()
        (self.root / "commands" / "go.md").write_text("x")
        kinds = {s["kind"]: s for s in page.tree(self.root)}
        self.assertEqual(list(kinds), ["main", "skills", "commands", "other"])
        inner = kinds["skills"]["entries"][0]
        self.assertTrue(inner["folder"])
        self.assertEqual(inner["preview"], "skills/inner/SKILL.md")
        self.assertEqual(inner["children"], [{"name": "SKILL.md", "path": "skills/inner/SKILL.md"}])

    def test_no_names_from_folders_outside_the_skill(self):
        private = self.home / "private"
        private.mkdir()
        (private / "plans.md").write_text("x")
        os.symlink(private, self.root / "references" / "linked")
        os.symlink(private, self.root / "commands")
        (self.root / "hooks").write_text("a plain file named like a section")
        names = [e["name"] for s in page.tree(self.root) for e in s["entries"]]
        self.assertNotIn("references/linked/plans.md", names)
        self.assertFalse(any("plans.md" in n for n in names))
        self.assertIn("hooks", names)

    def test_editable_only_under_quiver_outside_cache(self):
        self.assertTrue(page.editable(str(self.home / ".swe" / "skills" / "x"), self.home))
        self.assertFalse(page.editable(str(self.home / ".swe" / "cache" / "x"), self.home))
        self.assertFalse(page.editable(str(self.root), self.home))

    def test_file_view_stays_inside_the_folder(self):
        self.assertEqual(page.file_view(self.skill, "references/loop.md")["text"], "red, green")
        self.assertIsNone(page.file_view(self.skill, "logo.png")["text"])
        for name in ("escape.md", "../../../secret.txt"):
            view = page.file_view(self.skill, name)
            self.assertTrue(view["outside"], name)
            self.assertIsNone(view["text"])
        with self.assertRaises(PageError) as bad:
            page.file_view(self.skill, "/etc/passwd")
        self.assertEqual(bad.exception.status, 400)
        with self.assertRaises(PageError) as missing:
            page.file_view(self.skill, "nope.md")
        self.assertEqual(missing.exception.status, 404)

    def test_get_dispatches_by_args(self):
        inventory = {"shared_dir": "/s", "harnesses": [{"name": "codex"}], "skills": [self.skill]}
        ctx = SimpleNamespace(home=self.home, machine={"name": "box"})
        with patch.object(page, "_inventory", return_value=inventory):
            d = page.get(ctx, {"skill": str(self.root)})
            self.assertEqual((d["machine"], d["harnesses"]), ("box", [{"name": "codex"}]))
            f = page.get(ctx, {"skill": str(self.root), "file": "SKILL.md"})
            self.assertIn("# TDD", f["text"])
            with self.assertRaises(PageError) as unknown:
                page.get(ctx, {"skill": str(self.home)})
            self.assertEqual(unknown.exception.status, 404)


class TestPageScripts(unittest.TestCase):
    def test_agents_script_needs_nothing_from_skills_script(self):
        """The bundle runs every page script in its own scope, so
        agents.js can't call a helper skills.js defines, or the other way
        round (the AGENTS.md page once threw on skillsPath)."""
        # Read from the source: a call into the other script only fails
        # when that line runs, which a load under node would not reach.
        # A helper is a function declaration or a function-valued const,
        # let or var (an arrow or a function expression), at any depth.
        import re
        folder = page.Path(pages_dir())
        defs = re.compile(r"^\s*(?:async\s+)?function\s*\*?\s*(\w+)"
                          r"|^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\b\s*)?"
                          r"(?:function\b|\([^()]*\)\s*=>|\w+\s*=>)", re.M)
        scripts = {n: (folder / n).read_text() for n in ("agents.js", "skills.js")}
        defined = {n: {a or b for a, b in defs.findall(t)} for n, t in scripts.items()}
        for name in scripts:
            self.assertGreater(len(defined[name]), 5, f"no helpers found in {name}: the pattern is stale")
        for name, other in (("agents.js", "skills.js"), ("skills.js", "agents.js")):
            for fn in defined[other] - defined[name]:
                self.assertIsNone(re.search(rf"(?<![\w.$]){fn}\s*\(", scripts[name]),
                                  f"{name} calls {fn} from {other}")


def pages_dir():
    from swe.webapp import pages
    return pages.STATIC


class TestRoute(HubBase):
    def setUp(self):
        super().setUp()
        self.inventory = {
            "shared_dir": str(self.srv.home / ".swe" / "skills"),
            "harnesses": [{"name": "claude", "label": "claude", "path": "/c", "state": "linked",
                           "detail": "", "archived": False}],
            "skills": [_skill("lint", "/p/lint", source="plugin", harnesses=["claude"]),
                       _skill("tdd", "/h/tdd")],
        }
        patcher = patch("swe.skills.inventory.installed_skills", return_value=self.inventory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_needs_a_device_and_lists_standalone_skills(self):
        self.assertEqual(self.srv.request("GET", "/v1/app/skills")[0], 401)
        with patch("swe.plugins.catalog.build_catalog", return_value=_catalog("/p")):
            status, data = self.srv.request("GET", "/v1/app/skills", headers=self.auth())
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertEqual([(s["name"], s["plugin"]) for s in body["skills"]],
                         [("lint", "tools@market"), ("tdd", None)])
        self.assertEqual(body["plugin_skill_count"], 1)
        self.assertEqual(body["harnesses"][0]["state"], "linked")
        self.assertEqual(body["machine"], self.srv.state.machine["name"])

    def test_unknown_skill_is_a_404(self):
        status, data = self.srv.request("GET", "/v1/app/skills?skill=" + quote("/etc"), headers=self.auth())
        self.assertEqual(status, 404, data)

    def test_page_assets_are_bundled(self):
        status, data = self.srv.request("GET", "/pages.js")
        self.assertEqual(status, 200)
        self.assertIn(b".sk-row", self.srv.request("GET", "/pages.css")[1])



# -- writes ------------------------------------------------------------------


def _skills_home(home: Path) -> Path:
    """A home with one shared skill and a folder in each link state:
    codex has none yet (create), claude holds a skill found nowhere else
    (keep), cursor holds a copy of the shared one (absorb, needs --force)
    and kilo an empty folder (absorb, empty)."""
    shared = home / ".swe" / "skills" / "tdd"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("---\nname: tdd\ndescription: Test first\n---\n")
    (home / ".codex").mkdir()
    (home / ".claude" / "skills" / "mine").mkdir(parents=True)
    (home / ".claude" / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n")
    (home / ".cursor" / "skills" / "tdd").mkdir(parents=True)
    (home / ".cursor" / "skills" / "tdd" / "SKILL.md").write_text((shared / "SKILL.md").read_text())
    (home / ".kilo" / "skills").mkdir(parents=True)
    (home / ".config" / "kilo").mkdir(parents=True)
    return shared


class _WriteHome:
    """A real temp home, with skill_links.json (a path fixed at import,
    from the test runner's HOME) moved into it."""

    def make_home(self, home: Path):
        self.home = home
        self.shared = _skills_home(home)
        self.records = home / ".swe" / "config" / "skill_links.json"
        for target, value in (("swe.skills.layout.SKILL_LINKS_FILE", self.records),
                              ("swe.skills.layout.CONFIG_DIR", self.records.parent)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        catalog = patch("swe.plugins.catalog.build_catalog", return_value={"marketplaces": []})
        catalog.start()
        self.addCleanup(catalog.stop)

    def path(self, name):
        return str({"codex": self.home / ".codex" / "skills", "claude": self.home / ".claude" / "skills",
                    "cursor": self.home / ".cursor" / "skills", "kilo": self.home / ".kilo" / "skills"}[name])

    def states(self, reply):
        return {r["name"]: r["state"] for r in reply["harnesses"]}

    def links(self):
        return {r["label"] for r in json.loads(self.records.read_text())["links"]}


class TestWrites(_WriteHome, unittest.TestCase):
    def setUp(self):
        self.make_home(temp_dir(self))
        self.ctx = SimpleNamespace(home=self.home, machine={"name": "box"})

    def post(self, action, body):
        return page.post(self.ctx, action, body)

    def refused(self, action, body):
        with self.assertRaises(PageError) as caught:
            self.post(action, body)
        return caught.exception

    def test_link_creates_the_symlink_and_returns_the_new_states(self):
        reply = self.post("link", {"harness": self.path("codex"), "expect": "create", "skill": str(self.shared)})
        codex = Path(self.path("codex"))
        self.assertTrue(codex.is_symlink())
        self.assertEqual(codex.resolve(), self.shared.parent.resolve())
        self.assertEqual(reply["result"], {"action": "link", "harness": "codex", "source": str(codex),
                                           "target": str(self.shared.parent)})
        self.assertEqual(reply["harness"]["state"], "linked")
        self.assertEqual(self.states(reply)["codex"], "linked")
        self.assertEqual(reply["skill"]["path"], os.path.realpath(self.shared))
        self.assertIn("codex", reply["skill"]["harnesses"])
        self.assertEqual(reply["machine"], "box")
        self.assertIn("codex", self.links())

    def test_a_skill_named_through_a_symlink_is_the_listed_one(self):
        """The inventory lists realpaths; a path through a symlinked home
        (macOS's /var -> /private/var) still names the skill."""
        alias = Path(self.enterContext(tempfile.TemporaryDirectory())) / "home"
        alias.symlink_to(self.home)
        reply = self.post("link", {"harness": self.path("codex"), "skill": str(alias / ".swe" / "skills" / "tdd")})
        self.assertEqual(reply["skill"]["path"], os.path.realpath(self.shared))
        detail = page.get(self.ctx, {"skill": str(alias / ".swe" / "skills" / "tdd")})
        self.assertEqual(detail["skill"]["path"], os.path.realpath(self.shared))

    def test_a_symlink_to_a_folder_off_the_list_is_still_unknown(self):
        stray = Path(self.enterContext(tempfile.TemporaryDirectory())) / "stray"
        stray.mkdir()
        (stray / "SKILL.md").write_text("---\nname: stray\n---\n")
        (self.home / "via").symlink_to(stray)
        for ref in (str(self.home / "via"), str(stray), "/etc\0x"):
            with self.assertRaises(PageError) as caught:
                page.get(self.ctx, {"skill": ref})
            self.assertEqual((caught.exception.status, caught.exception.code), (404, "unknown_skill"), ref)

    def test_a_name_works_when_one_folder_has_it(self):
        reply = self.post("link", {"harness": "codex"})
        self.assertEqual(reply["harness"]["path"], self.path("codex"))
        self.assertIsNone(reply["skill"])

    def test_keep_is_never_overwritten(self):
        err = self.refused("link", {"harness": self.path("claude")})
        self.assertEqual((err.status, err.code), (409, "keep"))
        self.assertIn("exist nowhere else", err.message)
        claude = Path(self.path("claude"))
        self.assertFalse(claude.is_symlink())
        self.assertTrue((claude / "mine" / "SKILL.md").is_file())

    def test_conflict_is_refused_with_the_reason(self):
        kilo = Path(self.path("kilo"))
        kilo.rmdir()
        kilo.write_text("not a folder")
        err = self.refused("link", {"harness": str(kilo)})
        self.assertEqual((err.status, err.code), (409, "conflict"))
        self.assertIn("real file", err.message)
        self.assertEqual(kilo.read_text(), "not a folder")

    def test_copies_of_shared_skills_are_backed_up_then_linked(self):
        reply = self.post("link", {"harness": self.path("cursor"), "expect": "absorb"})
        self.assertEqual(self.states(reply)["cursor"], "linked")
        backups = list((self.home / ".swe" / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "tdd" / "SKILL.md").is_file())

    def test_a_folder_of_empty_subfolders_links(self):
        kilo = Path(self.path("kilo"))
        (kilo / "old" / "nested").mkdir(parents=True)
        reply = self.post("link", {"harness": str(kilo), "expect": "absorb", "expect_detail": "empty"})
        self.assertEqual(reply["harness"]["state"], "linked")
        self.assertTrue(kilo.is_symlink())
        reply = self.post("unlink", {"harness": str(kilo)})
        (kilo / "old").mkdir(parents=True)
        reply = self.post("link_all", {"harnesses": [str(kilo)]})
        self.assertEqual([r["name"] for r in reply["result"]["linked"]], ["kilo"])

    def test_a_folder_that_changed_since_the_page_read_it_is_left_alone(self):
        err = self.refused("link", {"harness": self.path("codex"), "expect": "absorb"})
        self.assertEqual((err.status, err.code), (409, "state_changed"))
        self.assertIn("No skills folder yet", err.message)
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_a_folder_that_filled_up_within_the_same_state_is_left_alone(self):
        # Confirmed as an empty folder; it now holds a copy of a shared
        # skill, so linking would back it up: ask again instead.
        kilo = Path(self.path("kilo"))
        (kilo / "tdd").mkdir()
        (kilo / "tdd" / "SKILL.md").write_text((self.shared / "SKILL.md").read_text())
        err = self.refused("link", {"harness": str(kilo), "expect": "absorb", "expect_detail": "empty"})
        self.assertEqual((err.status, err.code), (409, "state_changed"))
        self.assertIn("Linking backs it up", err.message)
        self.assertFalse(kilo.is_symlink())
        self.assertFalse((self.home / ".swe" / "backups").exists())
        reply = self.post("link", {"harness": str(kilo), "expect": "absorb",
                                   "expect_detail": "1 skills, all already shared"})
        self.assertEqual(reply["harness"]["state"], "linked")

    def test_unlink_removes_only_the_link(self):
        self.post("link", {"harness": self.path("codex")})
        reply = self.post("unlink", {"harness": self.path("codex"), "expect": "linked", "skill": str(self.shared)})
        self.assertFalse(os.path.lexists(self.path("codex")))
        self.assertTrue((self.shared / "SKILL.md").is_file())
        self.assertEqual(reply["result"], {"action": "unlink", "harness": "codex", "path": self.path("codex"),
                                           "mkdir": False})
        self.assertEqual(self.states(reply)["codex"], "create")
        self.assertNotIn("codex", reply["skill"]["harnesses"])
        self.assertNotIn("codex", self.links())

    def test_toggle_enable_makes_an_only_selection(self):
        reply = self.post("toggle", {"harness": self.path("codex"), "expect": "create",
                                     "skill": str(self.shared), "enabled": True})
        self.assertEqual(reply["result"]["action"], "enable")
        self.assertEqual(reply["harness"]["state"], "selection")
        self.assertEqual(reply["harness"]["mode"], "only")
        self.assertEqual(reply["harness"]["enabled"], ["tdd"])
        self.assertTrue(Path(self.path("codex"), "tdd").is_symlink())
        self.assertIn("codex", reply["skill"]["harnesses"])

    def test_toggle_disable_converts_a_linked_folder_to_except(self):
        self.post("link", {"harness": self.path("codex")})
        reply = self.post("toggle", {"harness": self.path("codex"), "expect": "linked",
                                     "skill": str(self.shared), "enabled": False})
        self.assertEqual(reply["result"]["mode"], "except")
        self.assertEqual(reply["harness"]["disabled"], ["tdd"])
        self.assertFalse(os.path.lexists(Path(self.path("codex")) / "tdd"))
        self.assertNotIn("codex", reply["skill"]["harnesses"])

    def test_toggle_enable_keeps_private_skills_beside_the_link(self):
        reply = self.post("toggle", {"harness": self.path("claude"), "expect": "keep",
                                     "skill": str(self.shared), "enabled": True})
        self.assertEqual(reply["harness"]["state"], "selection")
        self.assertTrue(Path(self.path("claude"), "tdd").is_symlink())
        self.assertTrue(Path(self.path("claude"), "mine", "SKILL.md").is_file())
        self.assertFalse(Path(self.path("claude"), "mine").is_symlink())

    def test_toggle_refuses_a_non_shared_skill_and_a_bad_flag(self):
        mine = _skill("mine", self.home / ".claude" / "skills" / "mine")
        err = self.refused("toggle", {"harness": self.path("codex"),
                                      "skill": mine["path"], "enabled": True})
        self.assertEqual((err.status, err.code), (409, "not_shared"))
        err = self.refused("toggle", {"harness": self.path("codex"),
                                      "skill": str(self.shared), "enabled": "yes"})
        self.assertEqual((err.status, err.code), (422, "bad_enabled"))

    def test_toggle_refuses_a_folder_swe_cannot_attach_to(self):
        kilo = Path(self.path("kilo"))
        kilo.rmdir()
        kilo.symlink_to(self.home / "elsewhere")
        err = self.refused("toggle", {"harness": str(kilo),
                                      "skill": str(self.shared), "enabled": True})
        self.assertEqual((err.status, err.code), (409, "relink"))

    def test_detail_carries_the_usage_shape(self):
        got = page.get(self.ctx, {"skill": str(self.shared)})
        # No index under the fake home: tracked false, not zero.
        self.assertEqual(got["usage"], {"tracked": False})

    def test_unlink_refuses_a_folder_that_is_not_linked(self):
        err = self.refused("unlink", {"harness": self.path("claude")})
        self.assertEqual((err.status, err.code), (409, "not_linked"))
        self.assertTrue(Path(self.path("claude"), "mine", "SKILL.md").is_file())

    def test_link_refuses_a_folder_already_linked(self):
        self.post("link", {"harness": self.path("codex")})
        self.assertEqual(self.refused("link", {"harness": self.path("codex")}).code, "already_linked")

    def test_link_all_takes_only_the_free_folders(self):
        reply = self.post("link_all", {"skill": str(self.shared)})
        linked = {r["name"] for r in reply["result"]["linked"]}
        self.assertIn("codex", linked)
        self.assertIn("kilo", linked)
        self.assertNotIn("cursor", linked)
        self.assertNotIn("claude", linked)
        self.assertEqual(reply["result"]["failed"], [])
        self.assertEqual({r["name"] for r in reply["result"]["skipped"]}, {"claude", "cursor"})
        states = self.states(reply)
        self.assertEqual((states["codex"], states["kilo"], states["claude"], states["cursor"]),
                         ("linked", "linked", "keep", "absorb"))
        self.assertFalse((self.home / ".swe" / "backups").exists())
        self.assertIn("kilo", reply["skill"]["harnesses"])

    def test_link_all_only_links_the_folders_the_confirm_named(self):
        reply = self.post("link_all", {"harnesses": [self.path("kilo"), self.path("claude")]})
        self.assertEqual([r["name"] for r in reply["result"]["linked"]], ["kilo"])
        self.assertEqual([f["name"] for f in reply["result"]["failed"]], ["claude"])
        self.assertIn("changed since", reply["result"]["failed"][0]["message"])
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_link_all_reports_a_named_folder_the_hub_no_longer_lists(self):
        gone = str(self.home / ".gone" / "skills")
        reply = self.post("link_all", {"harnesses": [self.path("kilo"), gone]})
        self.assertEqual([r["name"] for r in reply["result"]["linked"]], ["kilo"])
        self.assertEqual([(f["path"], f["message"]) for f in reply["result"]["failed"]],
                         [(gone, "no longer listed on the hub.")])
        self.assertFalse(os.path.lexists(gone))

    def test_archived_folders_are_left_out_of_link_all_but_link_on_a_click(self):
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True, exist_ok=True)
        (config / "harness.json").write_text(json.dumps({"codex": {"state": "archived"}}))
        reply = self.post("link_all", {})
        self.assertNotIn("codex", {r["name"] for r in reply["result"]["linked"]})
        self.assertFalse(os.path.lexists(self.path("codex")))
        codex = next(r for r in reply["harnesses"] if r["name"] == "codex")
        self.assertEqual((codex["state"], codex["archived"]), ("create", True))
        reply = self.post("link", {"harness": self.path("codex"), "expect": "create"})
        self.assertEqual(reply["harness"]["state"], "linked")

    def test_refusals_for_bad_input(self):
        for action, body, status, code in (
            ("link", {"harness": "/etc"}, 404, "unknown_harness"),
            ("link", {"harness": str(self.home / ".swe" / "skills")}, 404, "unknown_harness"),
            ("link", {}, 422, "bad_harness"),
            ("link", {"harness": 3}, 422, "bad_harness"),
            ("link", {"harness": self.path("codex"), "expect": 1}, 422, "bad_expect"),
            ("link", {"harness": self.path("codex"), "expect_detail": []}, 422, "bad_expect"),
            ("link", {"harness": self.path("codex"), "skill": "/etc"}, 404, "unknown_skill"),
            ("link_all", {"harnesses": "codex"}, 422, "bad_harnesses"),
            ("relink", {"harness": self.path("codex")}, 404, "unknown_action"),
        ):
            err = self.refused(action, body)
            self.assertEqual((err.status, err.code), (status, code), (action, body))
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_an_unreadable_records_file_stops_the_link_before_it_moves(self):
        self.records.parent.mkdir(parents=True, exist_ok=True)
        self.records.write_text("{not json")
        err = self.refused("link", {"harness": self.path("codex")})
        self.assertEqual((err.status, err.code), (409, "link_failed"))
        self.assertFalse(os.path.lexists(self.path("codex")))
        self.assertEqual(self.records.read_text(), "{not json")

    def _selection(self, name: str = "codex", private: bool = False) -> Path:
        """A skills root as a dir of per-skill links into shared."""
        root = Path(self.path(name))
        root.mkdir(parents=True)
        (root / "tdd").symlink_to(self.shared.parent / "tdd")
        if private:
            (root / "own").mkdir()
            (root / "own" / "SKILL.md").write_text("---\nname: own\n---\n")
        return root

    def test_a_selection_is_its_own_link_state(self):
        self._selection()
        reply = page.get(self.ctx, {})
        codex = next(r for r in reply["harnesses"] if r["name"] == "codex")
        self.assertEqual(codex["state"], "selection")
        self.assertEqual(page.link_state(codex), "selection")

    def test_link_a_pure_selection_relinks_the_whole_folder(self):
        root = self._selection()
        reply = self.post("link", {"harness": self.path("codex"), "expect": "selection"})
        self.assertTrue(root.is_symlink())
        self.assertEqual(self.states(reply)["codex"], "linked")

    def test_link_a_selection_with_privates_is_refused_without_force(self):
        root = self._selection(private=True)
        err = self.refused("link", {"harness": self.path("codex")})
        self.assertEqual((err.status, err.code), (409, "link_failed"))
        self.assertTrue(root.is_dir() and not root.is_symlink())
        self.assertTrue((root / "own" / "SKILL.md").is_file())

    def test_unlink_a_selection_drops_links_keeps_privates(self):
        root = self._selection(private=True)
        reply = self.post("unlink", {"harness": self.path("codex"), "expect": "selection"})
        self.assertFalse((root / "tdd").exists())
        self.assertTrue((root / "own" / "SKILL.md").is_file())
        # With only a private skill left the folder reads as keep again.
        self.assertEqual(self.states(reply)["codex"], "keep")

    def test_link_all_leaves_a_selection_alone(self):
        self._selection()
        reply = self.post("link_all", {})
        self.assertNotIn("codex", {r["name"] for r in reply["result"]["linked"]})
        self.assertNotIn("codex", {r["name"] for r in reply["result"]["skipped"]})
        self.assertFalse(Path(self.path("codex")).is_symlink())


class TestNewSkill(_WriteHome, unittest.TestCase):
    """New skill: swe skills new, writing ~/.swe/skills/<name>/SKILL.md."""

    def setUp(self):
        self.make_home(temp_dir(self))
        self.ctx = SimpleNamespace(home=self.home, machine={"name": "box"})

    def test_writes_the_skeleton_and_lists_it(self):
        reply = page.post(self.ctx, "new", {"name": "review-pr", "description": "When a PR needs review"})
        target = self.home / ".swe" / "skills" / "review-pr" / "SKILL.md"
        self.assertEqual(reply["created"], str(target))
        text = target.read_text()
        self.assertIn("name: 'review-pr'", text)
        self.assertIn("description: 'When a PR needs review'", text)
        self.assertEqual(reply["skill"]["name"], "review-pr")
        self.assertIn("review-pr", [s["name"] for s in reply["skills"]])
        self.assertEqual(reply["action"], "new")

    def test_a_blank_description_takes_the_default(self):
        page.post(self.ctx, "new", {"name": "plain", "description": "  "})
        text = (self.home / ".swe" / "skills" / "plain" / "SKILL.md").read_text()
        self.assertIn("Describe when an agent should use this skill.", text)

    def test_refusals_say_why(self):
        page.post(self.ctx, "new", {"name": "taken"})
        for body, words in (({"name": "Bad Name"}, "isn't a valid skill name"),
                            ({"name": "taken"}, "already exists"),
                            ({"name": "x", "description": "two\nlines"}, "one line"),
                            ({"name": 3}, "name must be"),
                            ({"name": "y", "description": ["no"]}, "description must be")):
            with self.assertRaises(PageError) as refused:
                page.post(self.ctx, "new", body)
            self.assertEqual((refused.exception.status, refused.exception.code), (422, "bad_skill"), body)
            self.assertIn(words, refused.exception.message, body)
        self.assertFalse((self.home / ".swe" / "skills" / "x").exists())


class TestExplanation(unittest.TestCase):
    def test_matches_the_app_and_link_all_targets(self):
        roots = [{"name": n, "path": f"/{n}", "state": st, "detail": dt, "archived": ar}
                 for n, st, dt, ar in (("a", "create", "", False), ("b", "absorb", "empty", False),
                                       ("c", "absorb", "2 skills, all already shared", False),
                                       ("d", "keep", "1 of 1 skills exist nowhere else", False),
                                       ("e", "conflict", "real file, needs --force", False),
                                       ("f", "create", "", True), ("g", "ignored", "linkignore", False),
                                       ("h", "linked", "", False), ("i", "selection", "", False))]
        roots[8].update({"mode": "except", "disabled": ["tdd"]})
        link, skipped = page.link_all_targets(roots)
        self.assertEqual([r["name"] for r in link], ["a", "b"])
        # A selection is curated, not a loose folder: neither list takes it.
        self.assertEqual([r["name"] for r in skipped], ["c", "d", "e"])
        self.assertEqual(page.explanation(roots[8]),
                         "A selection (1 off): it links every shared skill but the ones switched off.")
        self.assertEqual(page.explanation(roots[5]),
                         "No skills folder yet. Archived in swe, so swe init leaves it alone; linking here creates it.")
        self.assertEqual(page.explanation(roots[2]),
                         "Holds only copies of shared skills (2 skills, all already shared). Linking backs it up and replaces it.")
        self.assertEqual(page.explanation(roots[4]), "conflict: real file, needs --force")


class TestWriteRoute(_WriteHome, HubBase):
    def setUp(self):
        super().setUp()
        self.make_home(self.srv.home)

    def origin(self):
        return f"http://127.0.0.1:{self.srv.port}"

    def test_needs_a_device(self):
        status, _ = self.srv.request("POST", "/v1/app/skills/link", body={"harness": self.path("codex")})
        self.assertEqual(status, 401)
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_link_and_unlink_through_the_hub(self):
        auth = {**self.auth(), "Origin": self.origin()}
        status, data = self.srv.request("POST", "/v1/app/skills/link",
                                        body={"harness": self.path("codex"), "expect": "create"}, headers=auth)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["harness"]["state"], "linked")
        self.assertTrue(Path(self.path("codex")).is_symlink())
        _, data = self.srv.request("GET", "/v1/app/skills", headers=auth)
        self.assertEqual(self.states(json.loads(data))["codex"], "linked")
        status, data = self.srv.request("POST", "/v1/app/skills/unlink",
                                        body={"harness": self.path("codex")}, headers=auth)
        self.assertEqual(status, 200, data)
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_new_skill_through_the_hub(self):
        status, data = self.srv.request("POST", "/v1/app/skills/new", body={"name": "hub-skill"},
                                        headers={**self.auth(), "Origin": self.origin()})
        self.assertEqual(status, 200, data)
        self.assertTrue((self.srv.home / ".swe" / "skills" / "hub-skill" / "SKILL.md").is_file())
        status, _ = self.srv.request("POST", "/v1/app/skills/new", body={"name": "evil"},
                                     headers={**self.auth(), "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse((self.srv.home / ".swe" / "skills" / "evil").exists())

    def test_a_cross_site_origin_is_refused(self):
        status, data = self.srv.request("POST", "/v1/app/skills/link_all", body={},
                                        headers={**self.auth(), "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("bad_origin", data.decode())
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_refusals_carry_their_status(self):
        auth = self.auth()
        for action, body, want in (
            ("link", {"harness": self.path("claude")}, 409),
            ("link", {"harness": "/nope"}, 404),
            ("link", {"harness": ""}, 422),
            ("nuke", {}, 404),
        ):
            status, data = self.srv.request("POST", f"/v1/app/skills/{action}", body=body, headers=auth)
            self.assertEqual(status, want, (action, data))
            self.assertIn("message", json.loads(data)["error"])
        self.assertEqual(self.srv.request("POST", "/v1/app/skills/link", body=["codex"], headers=auth)[0], 400)

    def test_page_script_carries_the_writes(self):
        # The bundle carries skills.js whole, compiled on its own; what its
        # writes do is covered by the node tests of the page below.
        status, data = self.srv.request("GET", "/pages.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/* skills.js */", data)
        self.assertIn(b"//# sourceURL=/pages/skills.js", data)
        self.assertIn(b"/v1/app/skills/", data)



@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestFileSheetReplies(unittest.TestCase):
    """pages/skills.js's file sheet is one dialog on document.body: a read
    for skill A's file, closed while it loads, must not fill the sheet
    after the same file name in skill B was opened."""

    def test_a_late_reply_for_another_opening_is_dropped(self):
        script = Path(__file__).resolve().parents[1] / "src/swe/webapp/static/pages/skills.js"
        prelude = (
            (script.parents[1] / "format.js").read_text(encoding="utf-8") + "\n"
            "const esc = (s) => String(s); function registerPage() {}\n"
            "const sheet = { innerHTML: '', dataset: {}, open: false, showModal() { this.open = true; }, close() { this.open = false; } };\n"
            "const bodySheet = () => sheet;\n"
            "const answers = []; const api = () => new Promise((r) => answers.push(r));\n")
        body = ("(async () => {\n"
                "  const a = openSkillFile({ skill: { path: '/s/a' }, home: '' }, 'README.md');\n"
                "  sheet.close();\n"
                "  const b = openSkillFile({ skill: { path: '/s/b' }, home: '' }, 'README.md');\n"
                "  answers[1]({ status: 200, data: { text: 'from b' } }); await b;\n"
                "  answers[0]({ status: 200, data: { text: 'from a' } }); await a;\n"
                "  process.stdout.write(JSON.stringify([sheet.innerHTML.includes('from b'), sheet.innerHTML.includes('from a')]));\n"
                "})();\n")
        done = subprocess.run(["node", "-e", prelude + script.read_text(encoding="utf-8") + "\n" + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [True, False])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestFileSheetFailures(unittest.TestCase):
    """pages/skills.js's file sheet under node: a refused credential
    closes it at the gate, like the page's other reads, and an
    unreachable hub says so instead of "The hub answered 0."."""

    def read_file(self, reply):
        script = Path(__file__).resolve().parents[1] / "src/swe/webapp/static/pages/skills.js"
        prelude = (
            (script.parents[1] / "format.js").read_text(encoding="utf-8") + "\n"
            "const esc = (s) => String(s); function registerPage() {}\n"
            "globalThis.location = { hash: '#/skills?skill=%2Fs%2Fa' };\n"
            "const gates = []; function renderGate() { gates.push(1); }\n"
            "const sheet = { innerHTML: '', dataset: {}, open: false, showModal() { this.open = true; }, close() { this.open = false; } };\n"
            "const bodySheet = () => sheet;\n"
            "const api = async () => (" + json.dumps(reply) + ");\n"
            + app_js_function("failText"))
        body = ("openSkillFile({ skill: { path: '/s/a' }, home: '' }, 'README.md').then(() =>\n"
                "  process.stdout.write(JSON.stringify({ open: sheet.open, gates: gates.length,\n"
                "    error: (sheet.innerHTML.match(/<p class=\"error\">(.*?)<\\/p>/) || [])[1] || null })));\n")
        done = subprocess.run(["node", "-e", prelude + script.read_text(encoding="utf-8") + "\n" + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_refused_credential_goes_to_the_gate(self):
        self.assertEqual(self.read_file({"status": 401, "data": None}), {"open": False, "gates": 1, "error": None})

    def test_an_unreachable_hub_says_so(self):
        self.assertEqual(self.read_file({"status": 0, "data": None}), {
            "open": True, "gates": 0, "error": "Couldn't reach the hub. Check the connection, then try again."})


def _unreadable(case, folder: Path) -> None:
    """Make ``folder`` unlistable for the rest of the test."""
    if os.geteuid() == 0:
        case.skipTest("root reads any folder")
    folder.chmod(0)
    case.addCleanup(folder.chmod, 0o755)


class TestTreeEdges(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self) / "skill"
        self.root.mkdir()

    def test_a_value_glued_to_its_colon_is_read_whole(self):
        self.assertEqual(page.frontmatter("---\nname:tdd\n---\n"), [{"key": "name", "value": "tdd"}])

    def test_an_unreadable_file_has_no_text(self):
        (self.root / "a.md").write_text("x")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertIsNone(page.read_text(self.root / "a.md"))

    def test_a_section_subfolder_lists_its_files_but_not_its_folders(self):
        (self.root / "commands" / "git" / "deeper").mkdir(parents=True)
        (self.root / "commands" / "git" / "push.md").write_text("x")
        (self.root / "commands" / "git" / ".draft.md").write_text("x")
        (self.root / "commands" / ".hidden.md").write_text("x")
        outside = self.root.parent / "elsewhere"
        outside.mkdir()
        (outside / "leak.md").write_text("x")
        os.symlink(outside, self.root / "commands" / "linked")
        sections = page.tree(self.root)
        self.assertEqual([s["kind"] for s in sections], ["commands"])
        self.assertEqual(sections[0]["entries"],
                         [{"name": "git/push.md", "folder": False, "path": "commands/git/push.md"}])

    def test_unlistable_folders_drop_out_quietly(self):
        (self.root / "agents" / "locked").mkdir(parents=True)
        (self.root / "hooks").mkdir()
        (self.root / "hooks" / "pre.sh").write_text("x")
        (self.root / "notes").mkdir()
        (self.root / "notes" / "a.md").write_text("x")
        _unreadable(self, self.root / "agents" / "locked")
        _unreadable(self, self.root / "notes")
        sections = {s["kind"]: [e["name"] for e in s["entries"]] for s in page.tree(self.root)}
        self.assertEqual(sections, {"hooks": ["pre.sh"]})
        _unreadable(self, self.root / "hooks")
        self.assertEqual(page.tree(self.root), [])


class TestExplanationStates(unittest.TestCase):
    def test_every_state_has_its_sentence(self):
        for root, words in (
            ({"state": "linked"}, "Reads the shared skills in ~/.swe/skills."),
            ({"state": "linked", "archived": True}, "Reads the shared skills. Archived in swe"),
            ({"state": "relink", "detail": "points at /elsewhere"}, "Points somewhere else: /elsewhere."),
            ({"state": "relink"}, "Points somewhere else."),
            ({"state": "absorb", "detail": "empty"}, "An empty folder. Linking replaces it."),
            ({"state": "keep", "detail": "1 skill"}, "Holds skills that exist nowhere else (1 skill)."),
            ({"state": "ignored"}, "Ignored by swe init."),
            ({"state": "ignored", "detail": "linkignore"}, "Ignored: linkignore."),
            ({"state": "skipped"}, "The harness isn't installed."),
            ({"state": "mystery"}, "mystery"),
        ):
            self.assertTrue(page.explanation(root).startswith(words), (root, page.explanation(root)))

    def test_a_name_two_folders_share_is_refused(self):
        roots = [{"name": "codex", "path": "/a"}, {"name": "codex", "path": "/b"}]
        self.assertEqual(page._root(roots, "/b"), roots[1])
        with self.assertRaises(PageError) as caught:
            page._root(roots, "codex")
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "ambiguous_harness"))


class TestWriteFailures(_WriteHome, unittest.TestCase):
    def setUp(self):
        self.make_home(temp_dir(self))
        self.ctx = SimpleNamespace(home=self.home, machine={"name": "box"})

    def refused(self, action, body):
        with self.assertRaises(PageError) as caught:
            page.post(self.ctx, action, body)
        return caught.exception

    def test_a_body_that_is_not_an_object(self):
        err = self.refused("link", ["codex"])
        self.assertEqual((err.status, err.code), (422, "bad_body"))

    def test_a_folder_swe_resolves_elsewhere_is_not_linked(self):
        from swe.skills.layout import SkillLayoutError

        with patch("swe.skills.layout.resolve_root_ref", side_effect=SkillLayoutError("no such root")):
            err = self.refused("link", {"harness": self.path("codex")})
        self.assertEqual((err.status, err.code, err.message), (409, "link_failed", "no such root"))
        with patch("swe.skills.layout.resolve_root_ref", return_value=("codex", Path("/other/skills"))):
            err = self.refused("link", {"harness": self.path("codex")})
        self.assertEqual((err.status, err.code), (409, "link_failed"))
        self.assertIn("as /other/skills", err.message)
        self.assertFalse(os.path.lexists(self.path("codex")))

    def test_an_unlink_swe_refuses_says_why(self):
        from swe.skills.layout import SkillLayoutError

        page.post(self.ctx, "link", {"harness": self.path("codex")})
        with patch("swe.skills.link_ops.unlink_skill_root", side_effect=SkillLayoutError("busy")):
            err = self.refused("unlink", {"harness": self.path("codex")})
        self.assertEqual((err.status, err.code, err.message), (409, "unlink_failed", "busy"))
        self.assertTrue(os.path.islink(self.path("codex")))

    def test_link_all_reports_a_disk_error_and_goes_on(self):
        from swe.skills import link_ops

        real = link_ops.link_skill_root

        def flaky(path, **kw):
            if path == self.path("codex"):
                raise OSError("disk full")
            return real(path, **kw)

        with patch("swe.skills.link_ops.link_skill_root", side_effect=flaky):
            reply = page.post(self.ctx, "link_all", {})
        self.assertEqual(reply["result"]["failed"],
                         [{"name": "codex", "path": self.path("codex"), "message": "disk full"}])
        self.assertIn("kilo", {r["name"] for r in reply["result"]["linked"]})


if __name__ == "__main__":
    unittest.main()
