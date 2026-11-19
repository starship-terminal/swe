"""`swe mcp tools` over streamable HTTP: one POST per JSON-RPC message.

A fake server on a loopback port answers as a JSON body or as an event
stream, hands out a session id, paginates, and misbehaves on request, so
the probe's error wording is pinned without a network.
"""

import http.server
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest

from swe.mcp.probe import ProbeError, inspect_http
from tests._tempdir import temp_dir

PROJECT_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
TOOLS = [{"name": f"tool{i}", "description": f"does {i}"} for i in range(3)]


def _lower(headers) -> dict:
    """Header names lower-cased: urllib sends its own capitalisation."""
    return {k.lower(): v for k, v in headers.items()}


class _Fake(http.server.BaseHTTPRequestHandler):
    # Set per test on the server object: "json", "sse", "401", "redirect",
    # "html", "rpc-error".
    def log_message(self, *args):
        pass

    def _send(self, message, status=200):
        srv = self.server
        if srv.mode == "sse":
            noise = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
            body = "".join(f"event: message\ndata: {json.dumps(m)}\n\n"
                           for m in (noise, message)).encode()
            kind = "text/event-stream"
        else:
            body = json.dumps(message).encode()
            kind = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if message.get("id") == 1:
            self.send_header("Mcp-Session-Id", "sess-1")
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.server.seen.append(("DELETE", _lower(self.headers)))
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        srv = self.server
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.seen.append((message.get("method"), _lower(self.headers)))
        if srv.mode == "401":
            self.send_response(401)
            self.end_headers()
            return
        if srv.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "https://elsewhere.example/mcp")
            self.end_headers()
            return
        if srv.mode == "html":
            body = b"<html>hello</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        mid, method = message.get("id"), message.get("method")
        if method == "initialize":
            if srv.mode == "rpc-error":
                return self._send({"jsonrpc": "2.0", "id": mid,
                                   "error": {"code": -1, "message": "nope"}})
            return self._send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "fake", "version": "9"}}})
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            if cursor is None:
                result = {"tools": TOOLS[:2], "nextCursor": "p2"}
            else:
                result = {"tools": TOOLS[2:]}
            return self._send({"jsonrpc": "2.0", "id": mid, "result": result})


class HttpProbeTest(unittest.TestCase):
    def serve(self, mode):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
        server.mode, server.seen = mode, []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"

    def test_json_answers_pages_session_and_headers(self):
        server, url = self.serve("json")
        result = inspect_http(url, {"Authorization": "Bearer t"}, timeout_s=5)
        self.assertEqual(result["tools"], TOOLS)
        self.assertEqual(result["server"], {"name": "fake", "version": "9"})
        self.assertEqual(result["protocol_version"], "2025-03-26")
        methods = [m for m, _ in server.seen]
        self.assertEqual(methods, ["initialize", "notifications/initialized",
                                   "tools/list", "tools/list", "DELETE"])
        first, later = server.seen[0][1], server.seen[2][1]
        self.assertEqual(first["authorization"], "Bearer t")
        self.assertIn("text/event-stream", first["accept"])
        self.assertNotIn("mcp-session-id", first)
        self.assertEqual(later["mcp-session-id"], "sess-1")
        self.assertEqual(later["mcp-protocol-version"], "2025-03-26")

    def test_event_stream_answers_skip_other_messages(self):
        _, url = self.serve("sse")
        self.assertEqual(inspect_http(url, None, timeout_s=5)["tools"], TOOLS)

    def test_failures_say_what_happened(self):
        for mode, words in (("401", "sign-in"), ("redirect", "elsewhere.example"),
                            ("html", "not JSON"), ("rpc-error", "nope")):
            server, url = self.serve(mode)
            with self.assertRaises(ProbeError) as caught:
                inspect_http(url, {"Authorization": "Bearer t"}, timeout_s=5)
            self.assertIn(words, str(caught.exception), mode)
            # Nothing follows a failed initialize but closing a session the
            # server opened; a refused redirect is never followed.
            sent = [m for m, _ in server.seen if m != "DELETE"]
            self.assertEqual(sent, ["initialize"], mode)

    def test_unreachable_and_bad_urls(self):
        with self.assertRaises(ProbeError) as caught:
            inspect_http("http://127.0.0.1:1/mcp", None, timeout_s=2)
        self.assertIn("cannot reach", str(caught.exception))
        for url in ("", "ftp://x/y", "not a url"):
            with self.assertRaises(ProbeError):
                inspect_http(url, None)

    def run_mcp(self, home, *args):
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(PROJECT_SRC)
        return subprocess.run([sys.executable, "-m", "swe.mcp", *args],
                              env=env, capture_output=True, text=True)

    def test_command_lists_an_http_servers_tools_and_refuses_sse(self):
        _, url = self.serve("json")
        home = temp_dir(self)
        (home / ".swe" / "config").mkdir(parents=True)
        (home / ".swe" / "config" / "tools.json").write_text('{"claude": {}}\n')
        (home / ".claude.json").write_text('{"mcpServers": {}}\n')
        (home / ".swe" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "web": {"url": url}, "old": {"url": url, "type": "sse"}}}) + "\n")
        r = self.run_mcp(home, "tools", "web", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)["data"]
        self.assertEqual(data["transport"], "http")
        self.assertEqual([t["name"] for t in data["tools"]], ["tool0", "tool1", "tool2"])
        r = self.run_mcp(home, "tools", "old", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sse", json.loads(r.stdout)["data"]["error"])


if __name__ == "__main__":
    unittest.main()
