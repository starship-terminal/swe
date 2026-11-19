"""`swe mcp set` and `swe mcp rename`: change a hub server in place.

`set` rewrites fields of the hub entry and pushes the result to the
harnesses that already hold the server; `rename` moves the hub entry, the
`swe` block's records and each harness's own entry to a new name.
"""

import json
import os
import pathlib
import subprocess
import sys
import unittest

from tests._tempdir import temp_dir

MCP_MODULE = "swe.mcp"
PROJECT_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

OLD_URL = "https://mcp.example.com:8110/api/mcp"
NEW_URL = "https://mcp.example.com:8108/api/mcp"


def _mcp_env(home: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PROJECT_SRC) if not existing else f"{PROJECT_SRC}{os.pathsep}{existing}"
    )
    return env


class _McpHome(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        cfg = self.home / ".swe" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "tools.json").write_text(
            json.dumps({"claude": {"aliases": ["cc"]}, "cursor": {}}) + "\n")
        self.hub_file = self.home / ".swe" / "mcp.json"
        self.claude_file = self.home / ".claude.json"
        self.cursor_file = self.home / ".cursor" / "mcp.json"
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        self.hub_write({})
        self.claude_write({})
        self.cursor_file.write_text(json.dumps({"mcpServers": {}}) + "\n")

    def run_mcp(self, *args):
        return subprocess.run(
            [sys.executable, "-m", MCP_MODULE, *args],
            env=_mcp_env(self.home), capture_output=True, text=True)

    def data(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)["data"]

    def hub_write(self, servers, **extra):
        self.hub_file.write_text(json.dumps({"mcpServers": servers, **extra}) + "\n")

    def claude_write(self, servers):
        self.claude_file.write_text(json.dumps({"mcpServers": servers}) + "\n")

    def hub_data(self):
        return json.loads(self.hub_file.read_text())

    def hub(self):
        return self.hub_data()["mcpServers"]

    def claude(self):
        return json.loads(self.claude_file.read_text())["mcpServers"]

    def cursor(self):
        return json.loads(self.cursor_file.read_text())["mcpServers"]


class McpSetTest(_McpHome):
    def test_url_reaches_the_hub_and_every_holder(self):
        self.hub_write({"docs": {"url": OLD_URL}})
        self.claude_write({"docs": {"type": "http", "url": OLD_URL}})
        r = self.run_mcp("set", "docs", f"--url={NEW_URL}", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.data(r), {"name": "docs", "changed": True,
                                        "synced": ["claude"], "failed": []})
        self.assertEqual(self.hub()["docs"]["url"], NEW_URL)
        self.assertEqual(self.claude()["docs"]["url"], NEW_URL)
        # Never synced to cursor: an edit is not a reason to spread it.
        self.assertEqual(self.cursor(), {})

    def test_off_harness_stays_off_and_loses_its_stale_copy(self):
        self.hub_write({"docs": {"url": OLD_URL}}, swe={
            "disabled": {"cursor": ["docs"]},
            "disabled_defs": {"cursor": {"docs": {"url": OLD_URL}}}})
        r = self.run_mcp("set", "docs", f"--url={NEW_URL}", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.hub_data()["swe"]
        self.assertEqual(state["disabled"], {"cursor": ["docs"]})
        self.assertNotIn("disabled_defs", state)
        self.assertEqual(self.cursor(), {})
        # Back on, it is the hub's new definition that lands.
        r = self.run_mcp("enable", "docs", "--for=cursor", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cursor()["docs"]["url"], NEW_URL)

    def test_url_edit_refuses_while_a_command_only_harness_holds_it(self):
        cfg = self.home / ".swe" / "config" / "tools.json"
        cfg.write_text(json.dumps({"claude": {}, "claude-desktop": {}}) + "\n")
        desktop = (self.home / "Library" / "Application Support" / "Claude"
                   / "claude_desktop_config.json")
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(json.dumps({"mcpServers": {"gh": {"command": "npx"}}}) + "\n")
        self.hub_write({"gh": {"command": "npx"}})
        r = self.run_mcp("list", "--json")
        row = next(s for s in self.data(r)["servers"] if s["name"] == "gh")
        if "claude-desktop" not in row["harnesses"]:
            self.skipTest("Claude Desktop's config lives elsewhere on this platform")
        r = self.run_mcp("set", "gh", f"--url={NEW_URL}", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude-desktop", r.stderr)
        self.assertEqual(self.hub()["gh"], {"command": "npx"})

    def test_shared_server_whose_harness_copy_needs_no_change_is_synced(self):
        gateway_url = "http://127.0.0.1:8478/mcp/gh"
        self.hub_write({"gh": {"command": "npx", "shared": True}},
                       gateway={"enabled": True, "port": 8478})
        self.claude_write({"gh": {"type": "http", "url": gateway_url}})
        r = self.run_mcp("set", "gh", "--env=A=1", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.data(r)["synced"], ["claude"])
        self.assertEqual(self.claude()["gh"]["url"], gateway_url)

    def test_env_pairs_merge_and_unset(self):
        self.hub_write({"gh": {"command": "npx", "args": ["gh"],
                               "env": {"A": "1", "B": "2"}, "shared": True}})
        r = self.run_mcp("set", "gh", "--env=C=3", "--unset-env=A")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hub()["gh"], {"command": "npx", "args": ["gh"],
                                            "env": {"B": "2", "C": "3"},
                                            "shared": True})

    def test_command_replaces_a_url_and_back(self):
        self.hub_write({"s": {"url": OLD_URL, "type": "sse",
                              "headers": {"X": "y"}}})
        r = self.run_mcp("set", "s", "--", "npx", "-y", "srv", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hub()["s"], {"command": "npx",
                                           "args": ["-y", "srv", "--json"]})
        r = self.run_mcp("set", "s", f"--url={NEW_URL}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hub()["s"], {"url": NEW_URL})

    def test_unchanged_writes_nothing(self):
        self.hub_write({"docs": {"url": OLD_URL}})
        before = self.hub_file.read_text()
        r = self.run_mcp("set", "docs", f"--url={OLD_URL}", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.data(r)["changed"])
        self.assertEqual(self.hub_file.read_text(), before)

    def test_refusals(self):
        self.hub_write({"docs": {"url": OLD_URL}, "gh": {"command": "npx"}})
        for args in (["set", "docs"],
                     ["set", "nope", f"--url={NEW_URL}"],
                     ["set", "docs", "--env=A=1"],
                     ["set", "gh", "--header=A=1"],
                     ["set", "docs", "--bogus"],
                     ["set", "docs", "--env=noequals"],
                     ["set", "docs", f"--url={NEW_URL}", "--", "npx"]):
            r = self.run_mcp(*args)
            self.assertEqual(r.returncode, 1, args)
        self.assertEqual(self.hub()["docs"], {"url": OLD_URL})

    def test_missing_secret_refuses_before_the_hub_moves(self):
        self.hub_write({"gh": {"command": "npx"}})
        self.claude_write({"gh": {"command": "npx"}})
        r = self.run_mcp("set", "gh", "--env=TOKEN=${NOT_STORED}", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT_STORED", r.stderr)
        self.assertEqual(self.hub()["gh"], {"command": "npx"})

    def test_help(self):
        r = self.run_mcp("set", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("swe mcp set", r.stdout)


class McpRenameTest(_McpHome):
    def test_moves_hub_state_and_harness_entries(self):
        self.hub_write({"a": {"command": "x"}, "docs": {"url": OLD_URL},
                        "z": {"command": "y"}},
                       swe={"disabled": {"cursor": ["docs", "a"]},
                            "disabled_defs": {"cursor": {"docs": {"url": OLD_URL}}},
                            "master_off": {"docs": ["cursor"]},
                            "adopted_from": {"docs": "claude"}})
        self.claude_write({"docs": {"type": "http", "url": OLD_URL, "extra": 1}})
        r = self.run_mcp("rename", "docs", "atlas", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.data(r), {
            "name": "atlas", "renamed_from": "docs", "hub": True,
            "renamed_in": ["claude"], "failed_in": []})
        # The entry keeps its place in the file and its own keys.
        self.assertEqual(list(self.hub()), ["a", "atlas", "z"])
        self.assertEqual(self.claude(),
                         {"atlas": {"type": "http", "url": OLD_URL, "extra": 1}})
        self.assertEqual(self.hub_data()["swe"], {
            "disabled": {"cursor": ["atlas", "a"]},
            "disabled_defs": {"cursor": {"atlas": {"url": OLD_URL}}},
            "master_off": {"atlas": ["cursor"]},
            "adopted_from": {"atlas": "claude"}})

    def test_shared_server_gateway_url_follows_the_name(self):
        self.hub_write({"gh": {"command": "npx", "shared": True}},
                       gateway={"enabled": True, "port": 8478})
        self.claude_write({"gh": {"type": "http",
                                  "url": "http://127.0.0.1:8478/mcp/gh"}})
        r = self.run_mcp("rename", "gh", "github")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude()["github"]["url"],
                         "http://127.0.0.1:8478/mcp/github")

    def test_kept_copy_of_a_shared_server_follows_the_name(self):
        kept = {"type": "http", "url": "http://127.0.0.1:8478/mcp/gh"}
        self.hub_write({"gh": {"command": "npx", "shared": True}},
                       gateway={"enabled": True, "port": 8478},
                       swe={"disabled": {"claude": ["gh"]},
                            "disabled_defs": {"claude": {"gh": kept}}})
        r = self.run_mcp("rename", "gh", "github")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_mcp("enable", "github", "--for=claude", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claude()["github"]["url"],
                         "http://127.0.0.1:8478/mcp/github")

    @unittest.skipIf(os.geteuid() == 0, "root writes through a read-only directory")
    def test_unwritable_harness_leaves_every_name_alone(self):
        self.hub_write({"docs": {"url": OLD_URL}},
                       swe={"disabled": {"x": ["docs"]}})
        self.claude_write({"docs": {"type": "http", "url": OLD_URL}})
        entry = {"url": OLD_URL}
        self.cursor_file.write_text(json.dumps({"mcpServers": {"docs": entry}}) + "\n")
        self.cursor_file.parent.chmod(0o555)
        self.addCleanup(self.cursor_file.parent.chmod, 0o755)
        r = self.run_mcp("rename", "docs", "atlas", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.data(r), {
            "name": "docs", "renamed_from": "docs", "hub": False,
            "renamed_in": [], "failed_in": ["cursor"]})
        self.assertEqual(list(self.hub()), ["docs"])
        self.assertEqual(list(self.claude()), ["docs"])
        self.assertEqual(list(self.cursor()), ["docs"])
        self.assertEqual(self.hub_data()["swe"], {"disabled": {"x": ["docs"]}})

    def test_harness_only_server(self):
        self.claude_write({"solo": {"command": "x"}})
        r = self.run_mcp("rename", "solo", "duo", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.data(r)["hub"])
        self.assertEqual(self.claude(), {"duo": {"command": "x"}})
        self.assertEqual(self.hub(), {})

    def test_refusals_write_nothing(self):
        self.hub_write({"docs": {"url": OLD_URL}, "taken": {"command": "x"}})
        self.claude_write({"docs": {"type": "http", "url": OLD_URL},
                           "held": {"command": "y"}})
        for args in (["rename", "docs"],
                     ["rename", "docs", "taken"],
                     ["rename", "docs", "held"],
                     ["rename", "docs", "docs"],
                     ["rename", "docs", "-bad"],
                     ["rename", "docs", "has space"],
                     ["rename", "nope", "atlas"],
                     ["rename", "docs", "atlas", "--bogus"]):
            r = self.run_mcp(*args)
            self.assertEqual(r.returncode, 1, args)
        self.assertEqual(list(self.hub()), ["docs", "taken"])
        self.assertEqual(list(self.claude()), ["docs", "held"])

    def test_help(self):
        r = self.run_mcp("rename", "help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("swe mcp rename", r.stdout)


if __name__ == "__main__":
    unittest.main()
