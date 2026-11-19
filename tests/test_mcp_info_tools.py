"""`swe mcp info` and `swe mcp tools`: the per-server detail surface.

info assembles what swe already knows (files, mtimes, switches, health);
tools runs a one-shot initialize + tools/list handshake against the
configured command. The fake servers here are small Python scripts
speaking newline-delimited JSON-RPC, the stdio transport.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

MCP_MODULE = "swe.mcp"
PROJECT_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _mcp_env(home: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PROJECT_SRC) if not existing else f"{PROJECT_SRC}{os.pathsep}{existing}"
    )
    return env


# A stdio MCP server: answers initialize and tools/list, announces itself
# on stderr first (noise must not break the reply matching), and reports
# whether $PROBE_KEY was in its environment by offering a marker tool.
FAKE_SERVER = r"""
import json, os, sys
sys.stderr.write("fake-server starting\n")
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.1"}}}), flush=True)
    elif m.get("method") == "notifications/initialized":
        pass
    elif m.get("method") == "tools/list":
        tools = [{"name": "ping", "description": "Ping the server"},
                 {"name": "echo", "description": "Echo input"}]
        if os.environ.get("PROBE_KEY") == "sekrit":
            tools.append({"name": "saw-secret", "description": ""})
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "tools": tools}}), flush=True)
    elif "id" in m:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                          "error": {"code": -32601, "message": "no such method"}}),
              flush=True)
"""

# A server that prints a non-JSON banner before the protocol starts.
NOISY_SERVER = r"""
import json, sys
print("this line is not json")
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "noisy", "version": "1"}}}), flush=True)
    elif m.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "tools": [{"name": "one", "description": "d"}]}}), flush=True)
"""

# A server that exits as soon as it starts.
DYING_SERVER = 'import sys; sys.stderr.write("cannot load config\\n"); sys.exit(3)'

# A server that pages its tools: first page carries nextCursor, the
# second closes the list.
PAGING_SERVER = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "pager", "version": "1"}}}), flush=True)
    elif m.get("method") == "tools/list":
        cursor = (m.get("params") or {}).get("cursor")
        if cursor is None:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
                "tools": [{"name": "page-one", "description": ""}],
                "nextCursor": "more"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
                "tools": [{"name": "page-two", "description": ""}]}}),
                flush=True)
"""

# A server that floods stderr past the pipe buffer before answering —
# the probe must drain it or the write blocks and the wait times out.
FLOODING_SERVER = r"""
import json, sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "serverInfo": {"name": "floody", "version": "1"}}}), flush=True)
    elif m.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
            "tools": [{"name": "survived", "description": ""}]}}), flush=True)
"""


class McpInfoToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        cfg = self.home / ".swe" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "tools.json").write_text(
            json.dumps({"claude": {"aliases": ["cc"]}}) + "\n")
        self.hub_file = self.home / ".swe" / "mcp.json"
        self.claude_file = self.home / ".claude.json"
        self.hub_write({})
        self.claude_write({})
        self.scripts = self.home / "bin"
        self.scripts.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_mcp(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", MCP_MODULE, *args],
            env=_mcp_env(self.home), input=stdin,
            capture_output=True, text=True)

    def hub_write(self, servers, swe=None):
        data = {"mcpServers": servers}
        if swe is not None:
            data["swe"] = swe
        self.hub_file.write_text(json.dumps(data) + "\n")

    def claude_write(self, servers):
        self.claude_file.write_text(json.dumps({"mcpServers": servers}) + "\n")

    def script(self, name, source):
        path = self.scripts / name
        path.write_text(source)
        return str(path)

    def json_out(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["data"]

    # -- info ----------------------------------------------------------

    def test_info_json_reports_files_switches_and_health(self):
        self.claude_write({"gh": {"command": "cat", "args": ["-n"],
                                  "env": {"API_KEY": "x"}}})
        self.hub_write({"gh": {"command": "cat", "args": ["-n"],
                               "env": {"API_KEY": "${GH_KEY}"}},
                            "other": {"url": "https://x.example/mcp"}})
        r = self.run_mcp("info", "gh", "--json")
        d = self.json_out(r)
        self.assertEqual(d["name"], "gh")
        self.assertEqual(d["transport"], "stdio")
        self.assertEqual(sorted(d["env_keys"]), ["API_KEY"])
        self.assertIn("claude", d["harnesses"])
        self.assertTrue(d["in_hub"])
        self.assertFalse(d["shared"])
        self.assertEqual(d["health"], "ok")
        self.assertEqual(d["secrets_missing"], ["GH_KEY"])
        places = {p["place"]: p for p in d["places"]}
        self.assertEqual(places["claude"]["state"], "on")
        self.assertEqual(places["swe"]["state"], "hub")
        self.assertTrue(places["claude"]["path"].endswith(".claude.json"))
        self.assertIsNotNone(d["updated"])
        self.assertIsNone(d["gateway"])

    def test_info_marks_disabled_places_off(self):
        self.hub_write({"gh": {"command": "cat", "args": []}},
                       swe={"disabled": {"claude": ["gh"]}})
        r = self.run_mcp("info", "gh", "--json")
        d = self.json_out(r)
        self.assertEqual(d["disabled_in"], ["claude"])
        places = {p["place"]: p for p in d["places"]}
        self.assertEqual(places["claude"]["state"], "off")
        self.assertEqual(places["swe"]["state"], "hub")

    def test_info_unknown_server_is_a_refusal(self):
        r = self.run_mcp("info", "nope", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unknown server", r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_info_text_names_places_and_state(self):
        self.claude_write({"gh": {"command": "cat", "args": []}})
        self.hub_write({"gh": {"command": "cat", "args": []}})
        r = self.run_mcp("info", "gh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gh", r.stdout)
        self.assertIn("claude", r.stdout)
        self.assertIn("Hub", r.stdout)

    # -- tools ----------------------------------------------------------

    def test_tools_json_lists_the_handshake_result(self):
        path = self.script("fake.py", FAKE_SERVER)
        self.claude_write({"fake": {"command": sys.executable, "args": [path]}})
        d = self.json_out(self.run_mcp("tools", "fake", "--json"))
        self.assertEqual(d["server"], {"name": "fake", "version": "0.1"})
        self.assertEqual(d["protocol_version"], "2025-06-18")
        self.assertEqual([t["name"] for t in d["tools"]], ["ping", "echo"])
        self.assertIsNone(d["error"])

    def test_tools_resolves_secrets_into_the_child_env(self):
        secrets = self.home / ".swe" / "secrets" / "mcp"
        secrets.mkdir(parents=True)
        (secrets / ".api_keys").write_text("PROBE_KEY=sekrit\n")
        path = self.script("fake.py", FAKE_SERVER)
        # The env ref lives in the hub copy the probe prefers.
        self.hub_write({"fake": {"command": sys.executable, "args": [path],
                                 "env": {"PROBE_KEY": "${PROBE_KEY}"}}})
        self.claude_write({"fake": {"command": sys.executable, "args": [path]}})
        d = self.json_out(self.run_mcp("tools", "fake", "--json"))
        self.assertIn("saw-secret", [t["name"] for t in d["tools"]])

    def test_tools_skips_non_json_stdout_noise(self):
        path = self.script("noisy.py", NOISY_SERVER)
        self.claude_write({"noisy": {"command": sys.executable, "args": [path]}})
        d = self.json_out(self.run_mcp("tools", "noisy", "--json"))
        self.assertEqual([t["name"] for t in d["tools"]], ["one"])

    def test_tools_reports_a_dying_server_with_stderr(self):
        path = self.script("dying.py", DYING_SERVER)
        self.claude_write({"dying": {"command": sys.executable, "args": [path]}})
        r = self.run_mcp("tools", "dying", "--json")
        self.assertEqual(r.returncode, 1)
        d = json.loads(r.stdout)["data"]
        self.assertIn("exited", d["error"])
        self.assertIn("cannot load config", d["error"])

    def test_tools_refuses_a_legacy_sse_server(self):
        # A streamable-http server is probed (tests/test_mcp_probe_http.py);
        # the older sse transport is still out of reach.
        self.claude_write({"web": {"url": "https://x.example/sse", "type": "sse"}})
        r = self.run_mcp("tools", "web", "--json")
        self.assertEqual(r.returncode, 1)
        d = json.loads(r.stdout)["data"]
        self.assertIn("sse transport", d["error"])

    def test_tools_follows_next_cursor_pages(self):
        path = self.script("paging.py", PAGING_SERVER)
        self.claude_write({"pager": {"command": sys.executable, "args": [path]}})
        d = self.json_out(self.run_mcp("tools", "pager", "--json"))
        self.assertEqual([t["name"] for t in d["tools"]],
                         ["page-one", "page-two"])

    def test_tools_survives_a_stderr_flood(self):
        path = self.script("flooding.py", FLOODING_SERVER)
        self.claude_write({"floody": {"command": sys.executable, "args": [path]}})
        d = self.json_out(self.run_mcp("tools", "floody", "--json",
                                       "--timeout=20"))
        self.assertEqual([t["name"] for t in d["tools"]], ["survived"])

    def test_tools_unknown_server_is_a_refusal(self):
        r = self.run_mcp("tools", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unknown server", r.stdout + r.stderr)



class ProbeDeadServerTest(unittest.TestCase):
    """A server that dies before the first write: the probe must name the
    exit and quote stderr, not the broken pipe the death caused. The
    command-level test above only hits this when the child loses a race."""

    @staticmethod
    def _close(pipe):
        try:
            pipe.close()
        except OSError:
            pass  # the write end of a dead child's pipe

    def test_write_to_an_exited_server_reports_the_exit(self):
        import subprocess as sp
        from swe.mcp import probe

        proc = sp.Popen(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('cannot load config'); sys.exit(3)"],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            self.addCleanup(self._close, pipe)
        proc.wait(timeout=10)
        errbuf = probe._StderrTail()
        with self.assertRaises(probe.ProbeError) as caught:
            # Larger than a pipe buffer is not needed: the reader is gone.
            probe._send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, errbuf)
        message = str(caught.exception)
        self.assertIn("exited (status 3)", message)
        self.assertIn("cannot load config", message)
        self.assertNotIn("Broken pipe", message)


if __name__ == "__main__":
    unittest.main()
