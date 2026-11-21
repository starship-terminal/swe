"""hub.py: auth, pairing route, REST endpoints, SSE stream."""

import http.client
import io
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from swe.remote import protocol
from swe.remote import tail
from swe.remote.hub import APPROVAL_TTL_S, HubState, make_server
from swe.remote import catalog as catalog_mod, hub as hub_mod, pairing
from swe.sessions.models import Session

from tests._tempdir import temp_dir


def _session(sid="s1", tool="claude", path="/tmp/proj", ts=0):
    """``ts`` is seconds since the epoch; Session keeps milliseconds."""
    return Session(timestamp=int(ts * 1000), agent="Claude Code", path=path,
                   title="test", session_id=sid, tool_name=tool)


class _Server:
    def __init__(self, sessions=None, statuses=None, home=None,
                 runtime=None):
        """``home`` is a folder the caller owns (a second hub over the
        same state); ``runtime`` goes to ``HubState`` (the session
        runtime's options)."""
        # Removed by stop(), or when the object goes if a test never stops it.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True) \
            if home is None else None
        self.home = Path(home) if home is not None else Path(self._tmp.name)
        self.state = HubState(self.home, runtime=runtime)
        self.state.catalog._fetch = lambda: list(sessions or [])
        self.state.catalog._status_of = (
            statuses or (lambda ss: ["active"] * len(ss))
        )
        # No tailnet in tests; cases that need an identity stub it.
        self.state.whois = lambda ip: None
        # Nor the network: pages price from the bundled LiteLLM snapshot
        # instead of downloading the live list on the first ledger read.
        self.state.webapp.download_prices = False
        self.state.catalog.refresh()
        self.state.tail_manager.sync(self.state.catalog.tail_rows())
        self.server = make_server(self.state, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        # shutdown() waits for the next poll; the default 0.5 s is paid by
        # every test that builds a server.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True
        )
        self.thread.start()

    def stop(self):
        # The state first: a queued send or when_idle detach left waiting
        # would otherwise keep polling this test's state long after it.
        self.state.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self._tmp is not None:
            self._tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path,
                         body=json.dumps(body) if body is not None else None,
                         headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            conn.close()

    def pair(self, role=""):
        token = pairing.mint_pair_token(self.home)
        body = {"token": token, "name": "test"}
        if role:
            body["role"] = role
        status, data = self.request("POST", "/v1/pair", body)
        assert status == 200, data
        return json.loads(data)

    def satellite(self, mid):
        """Pair a satellite credential and bind it to ``mid`` via
        /v1/agent/register; returns its Authorization headers."""
        cred = self.pair(role="satellite")
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        status, data = self.request(
            "POST", "/v1/agent/register", {"machine": {"id": mid}},
            headers=auth)
        assert status == 200, data
        return auth


class HubBase(unittest.TestCase):
    def setUp(self):
        self.srv = _Server(sessions=[_session()])
        self.addCleanup(self.srv.stop)

    def auth(self):
        cred = self.srv.pair()
        return {"Authorization": f"Bearer {cred['credential']}"}


class TestAuth(HubBase):
    def test_client_request_ends_low_power_but_status_does_not(self):
        self.srv.state._client_seen_at = float("-inf")
        self.srv.state.tail_manager.set_low_power(True)
        self.srv.request("GET", "/v1/status")
        self.assertTrue(self.srv.state.tail_manager.low_power)
        status, _ = self.srv.request("GET", "/v1/sessions",
                                     headers=self.auth())
        self.assertEqual(status, 200)
        self.assertFalse(self.srv.state.tail_manager.low_power)
        self.assertTrue(self.srv.state.watched())

    def test_status_is_public(self):
        status, data = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_status_reports_only_ok_and_version(self):
        status, data = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        # Machine id/name, swe version and uptime would name the box
        # for the whole tailnet; the probe only proves the hub is up
        # and says what it speaks (tests/test_remote_sse_v2.py).
        body = json.loads(data)
        self.assertEqual(
            body,
            {"ok": True, "v": protocol.PROTOCOL_VERSION,
             "protocols": [1, 2], "features": [
                 f for f in protocol.FEATURES
                 if f not in protocol.STORE_FEATURES],
             "boot_id": self.srv.state.boot_id},
        )
        self.assertNotIn(self.srv.state.machine_id, data.decode())

    def test_sessions_requires_auth(self):
        status, _ = self.srv.request("GET", "/v1/sessions")
        self.assertEqual(status, 401)
        status, data = self.srv.request(
            "GET", "/v1/sessions", headers=self.auth()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)["sessions"]), 1)

    def test_pair_rejects_bad_token(self):
        status, _ = self.srv.request(
            "POST", "/v1/pair", {"token": "bogus"}
        )
        self.assertEqual(status, 403)

    def test_revoked_device_is_401(self):
        cred = self.srv.pair()
        pairing.revoke_device(self.srv.home, cred["device_id"])
        status, _ = self.srv.request(
            "GET", "/v1/sessions",
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 401)


class TestEndpoints(HubBase):
    def test_session_row_shape(self):
        _, data = self.srv.request("GET", "/v1/sessions", headers=self.auth())
        row = json.loads(data)["sessions"][0]
        self.assertEqual(row["key"], "claude:s1")
        self.assertEqual(row["machine_id"], self.srv.state.machine_id)
        self.assertEqual(row["ownership"], "observed")
        self.assertIn("resume", row)

    def test_write_paths_reject_observed(self):
        auth = self.auth()
        for route, body in (
            ("message", {"text": "hi"}),
            ("approval", {"request_id": "r", "decision": "allow"}),
            ("control", {"op": "interrupt"}),
        ):
            status, data = self.srv.request(
                "POST", f"/v1/sessions/claude:s1/{route}",
                body=body, headers=auth,
            )
            self.assertEqual(status, 409, (route, data))
            self.assertEqual(
                json.loads(data)["error"]["code"], "not_attached"
            )

    def test_unknown_session_404(self):
        status, _ = self.srv.request(
            "POST", "/v1/sessions/claude:nope/message",
            body={"text": "x"}, headers=self.auth(),
        )
        self.assertEqual(status, 404)

    def test_messages_backfill(self):
        key = "claude:s1"
        self.srv.state.emit(
            key, protocol.event(
                "message.appended", key, "mbp",
                protocol.message("assistant", "hello"),
            )
        )
        status, data = self.srv.request(
            "GET", f"/v1/sessions/claude:s1/messages",
            headers=self.auth(),
        )
        self.assertEqual(status, 200)
        msgs = json.loads(data)["messages"]
        self.assertEqual(msgs[0]["data"]["text"], "hello")

    def test_inbox(self):
        status, data = self.srv.request(
            "GET", "/v1/inbox", headers=self.auth()
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["items"], [])


class _AttachedServer(_Server):
    """Hub with one session already marked supervised (headless)."""

    def __init__(self, **kw):
        super().__init__(sessions=[_session()],
                         statuses=lambda ss: ["done"])
        from swe.remote import supervise
        supervise.mark(self.home, "claude:s1", {
            "mode": "headless", "tool_name": "claude",
            "session_id": "s1", "cwd": "/tmp/proj", "attached_at": 0,
        })
        self.state.catalog.refresh()
        self.state.tail_manager.sync(self.state.catalog.tail_rows())


class TestHubWriteRoutes(unittest.TestCase):
    def setUp(self):
        self.srv = _AttachedServer()
        self.addCleanup(self.srv.stop)
        cred = self.srv.pair()
        self.hdr = {"Authorization": f"Bearer {cred['credential']}"}
        row = self.srv.state.catalog.row_for("claude:s1")
        self.assertEqual(row["ownership"], "attached")


    def test_unhashable_body_fields_answer_422(self):
        # These used to raise TypeError in the handler, which dropped
        # the connection with no response at all.
        for action, body in (
            ("message", {"text": "hi", "mode": []}),
            ("message", {"text": "hi", "mode": {}}),
            ("approval", {"request_id": "r1", "decision": []}),
            ("approval", {"request_id": "r1", "decision": {}}),
            ("control", {"op": []}),
            ("control", {"op": {}}),
        ):
            with self.subTest(action=action, body=body):
                status, data = self.srv.request(
                    "POST", f"/v1/sessions/claude:s1/{action}",
                    body=body, headers=self.hdr,
                )
                self.assertEqual(status, 422, data)
                self.assertEqual(
                    json.loads(data)["error"]["code"], "bad_command"
                )
    def test_message_success(self):
        from swe.remote import inject
        q = self.srv.state.bus.subscribe("claude:s1")
        with patch.object(
            inject.ClaudeHeadlessInjector, "_run",
            return_value=(True, "turn complete"),
        ):
            status, data = self.srv.request(
                "POST", "/v1/sessions/claude:s1/message",
                body={"text": "hi", "mode": "steer"}, headers=self.hdr,
            )
            self.assertEqual(status, 202, data)
            ev = q.get(timeout=5)
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(ev["type"], "command.result")
        self.assertTrue(ev["data"]["ok"])

    def test_message_bad_command_and_body(self):
        status, _ = self.srv.request(
            "POST", "/v1/sessions/claude:s1/message",
            body={"text": ""}, headers=self.hdr,
        )
        self.assertEqual(status, 422)
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request(
                "POST", "/v1/sessions/claude:s1/message",
                body="{not json", headers=self.hdr,
            )
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()

    def test_message_unknown_attachment(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude:s1/message",
            body={"text": "hi", "attachments": ["nope.png"]},
            headers=self.hdr,
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            json.loads(data)["error"]["code"], "bad_attachment"
        )

    def test_message_send_failure_lands_in_result(self):
        # The POST itself still answers 202; the injector's failure lands
        # as a command.result event on the session key.
        from swe.remote import inject
        q = self.srv.state.bus.subscribe("claude:s1")
        with patch.object(
            inject.ClaudeHeadlessInjector, "_run",
            return_value=(False, "claude exploded"),
        ):
            status, data = self.srv.request(
                "POST", "/v1/sessions/claude:s1/message",
                body={"text": "go"}, headers=self.hdr,
            )
            self.assertEqual(status, 202, data)
            ev = q.get(timeout=5)
        self.assertFalse(ev["data"]["ok"])
        self.assertIn("claude exploded", ev["data"]["note"])

    def test_approval_unsupported_headless(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude:s1/approval",
            body={"request_id": "r1", "decision": "allow"},
            headers=self.hdr,
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(data)["error"]["code"], "approval_not_supported"
        )

    def test_control_ops(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude:s1/control",
            body={"op": "compact"}, headers=self.hdr,
        )
        # headless rejects TUI-only ops
        self.assertEqual(status, 409)
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude:s1/control",
            body={"op": "detach"}, headers=self.hdr,
        )
        self.assertEqual(status, 200, data)
        row = self.srv.state.catalog.row_for("claude:s1")
        self.assertEqual(row["ownership"], "observed")
        # now writes are gated again
        status, _ = self.srv.request(
            "POST", "/v1/sessions/claude:s1/message",
            body={"text": "hi"}, headers=self.hdr,
        )
        self.assertEqual(status, 409)

    def test_attach_idempotent_and_modes(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude:s1/control",
            body={"op": "attach"}, headers=self.hdr,
        )
        self.assertEqual(status, 200)  # already attached

    def test_attach_not_attachable(self):
        # gemini has no resume flags → attachable False
        srv = _Server(
            sessions=[_session(tool="gemini", sid="g1")],
            statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, data = srv.request(
            "POST", "/v1/sessions/gemini:g1/control",
            body={"op": "attach"},
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(
            json.loads(data)["error"]["code"], "not_attachable"
        )

    def test_attach_headless_bad_mode(self):
        srv = _Server(
            sessions=[_session(tool="codex", sid="c9")],
            statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, _ = srv.request(
            "POST", "/v1/sessions/codex:c9/control",
            body={"op": "attach", "mode": "headless"},
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 422)

    def test_attach_headless_success(self):
        srv = _Server(
            sessions=[_session(sid="hx")], statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        with patch("shutil.which", return_value="/x/claude"), \
             patch("swe.remote.supervise._stop_local_writers"):
            status, data = srv.request(
                "POST", "/v1/sessions/claude:hx/control",
                body={"op": "attach", "mode": "headless"},
                headers={"Authorization": f"Bearer {cred['credential']}"},
            )
        self.assertEqual(status, 200, data)

    def test_attach_unknown_mode_422(self):
        srv = _Server(
            sessions=[_session(sid="bm")], statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, data = srv.request(
            "POST", "/v1/sessions/claude:bm/control",
            body={"op": "attach", "mode": "bogus"},
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(data)["error"]["code"], "bad_mode")

    def test_attach_serve_needs_opencode(self):
        srv = _Server(
            sessions=[_session(sid="sc")], statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, data = srv.request(
            "POST", "/v1/sessions/claude:sc/control",
            body={"op": "attach", "mode": "serve"},
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 422)
        error = json.loads(data)["error"]
        self.assertEqual(error["code"], "bad_mode")
        self.assertIn("opencode", error["message"])

    def test_attach_appserver_needs_codex(self):
        srv = _Server(
            sessions=[_session(sid="ac")], statuses=lambda ss: ["done"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, data = srv.request(
            "POST", "/v1/sessions/claude:ac/control",
            body={"op": "attach", "mode": "appserver"},
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        self.assertEqual(status, 422)
        error = json.loads(data)["error"]
        self.assertEqual(error["code"], "bad_mode")
        self.assertIn("codex", error["message"])

    def test_attachment_staging(self):
        import http.client as hc
        conn = hc.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/v1/sessions/claude:s1/attachments?name=note.txt",
                body=b"hello file",
                headers={
                    "Authorization": self.hdr["Authorization"],
                    "Content-Type": "application/octet-stream",
                },
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read())
            self.assertEqual(resp.status, 200, payload)
            self.assertIn("ref", payload)
        finally:
            conn.close()
        # extension cap
        conn = hc.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/v1/sessions/claude:s1/attachments?name=evil.exe",
                body=b"MZ",
                headers={
                    "Authorization": self.hdr["Authorization"],
                    "Content-Type": "application/octet-stream",
                },
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 422)
        finally:
            conn.close()

    def test_attachment_multipart_upload(self):
        boundary = "TESTBOUNDARY"
        blob = (
            b"--TESTBOUNDARY\r\n"
            b'Content-Disposition: form-data; name="file"; '
            b'filename="shot.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
            b"\x89PNG-DATA\r\n--TESTBOUNDARY--\r\n"
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request(
                "POST", "/v1/sessions/claude:s1/attachments",
                body=blob,
                headers={
                    "Authorization": self.hdr["Authorization"],
                    "Content-Type":
                        f"multipart/form-data; boundary={boundary}",
                },
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read())
            self.assertEqual(resp.status, 200, payload)
            self.assertTrue(payload["ref"].endswith("shot.png"))
        finally:
            conn.close()

    def test_multipart_keeps_trailing_dash_newline_bytes(self):
        # The old parser rstripped "\r\n-" from the tail, corrupting
        # content that legitimately ended in dash or newline bytes.
        from swe.remote import attachments as att

        boundary = "KEEPBYTES"
        content = b"log tail ends with dash-\r\n"
        blob = (
            b"--KEEPBYTES\r\n"
            b'Content-Disposition: form-data; name="file"; '
            b'filename="note.log"\r\n'
            b"Content-Type: text/plain\r\n\r\n" + content +
            b"\r\n--KEEPBYTES--\r\n"
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request(
                "POST", "/v1/sessions/claude:s1/attachments",
                body=blob,
                headers={
                    "Authorization": self.hdr["Authorization"],
                    "Content-Type":
                        f"multipart/form-data; boundary={boundary}",
                },
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read())
            self.assertEqual(resp.status, 200, payload)
        finally:
            conn.close()
        staged = att.resolve(
            self.srv.state.home, "claude:s1", payload["ref"]
        )
        self.assertIsNotNone(staged)
        self.assertEqual(Path(staged).read_bytes(), content)

    def test_multipart_boundary_with_extra_params(self):
        # `boundary=X; charset=utf-8` must extract only X — the old
        # split-on-"boundary=" swallowed the trailing params into the
        # marker and no part ever matched.
        boundary = "PARAMS123"
        blob = (
            b"--PARAMS123\r\n"
            b'Content-Disposition: form-data; name="file"; '
            b'filename="a.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"hello\r\n--PARAMS123--\r\n"
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request(
                "POST", "/v1/sessions/claude:s1/attachments",
                body=blob,
                headers={
                    "Authorization": self.hdr["Authorization"],
                    "Content-Type":
                        f'multipart/form-data; boundary={boundary}; '
                        'charset=utf-8',
                },
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read())
            self.assertEqual(resp.status, 200, payload)
            self.assertTrue(payload["ref"].endswith("a.txt"))
        finally:
            conn.close()

    def test_inbox_item_shape(self):
        srv = _Server(
            sessions=[_session(sid="f1", ts=time.time())],
            statuses=lambda ss: ["followup"],
        )
        self.addCleanup(srv.stop)
        cred = srv.pair()
        status, data = srv.request(
            "GET", "/v1/inbox",
            headers={"Authorization": f"Bearer {cred['credential']}"},
        )
        items = json.loads(data)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "followup")
        self.assertEqual(items[0]["key"], "claude:f1")

    def test_inbox_takes_a_satellite_row_missing_fields(self):
        """An older satellite may push a row without agent or title; the
        inbox lists it rather than answering 500."""
        srv = _Server()
        self.addCleanup(srv.stop)
        catalog_mod.merge_remote_rows(srv.home, "sat1", [{
            "key": "claude:x", "status": "followup",
            "last_active_at": time.time()}])
        srv.state.catalog.refresh()
        cred = srv.pair()
        status, data = srv.request(
            "GET", "/v1/inbox",
            headers={"Authorization": f"Bearer {cred['credential']}"})
        self.assertEqual(status, 200, data)
        [item] = [i for i in json.loads(data)["items"]
                  if i["key"] == "claude:x@sat1"]
        self.assertIsNone(item["harness"])

    def test_a_route_bug_answers_500_instead_of_dropping(self):
        srv = _Server()
        self.addCleanup(srv.stop)
        cred = srv.pair()
        hdr = {"Authorization": f"Bearer {cred['credential']}"}
        with patch.object(srv.state.catalog, "rows",
                          side_effect=RuntimeError("boom")), \
                patch("traceback.print_exc"):
            status, data = srv.request("GET", "/v1/sessions", headers=hdr)
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(data)["error"]["code"], "internal")
        status, _ = srv.request("GET", "/v1/sessions", headers=hdr)
        self.assertEqual(status, 200)

    def test_a_client_that_hung_up_is_not_logged(self):
        srv = _Server()
        self.addCleanup(srv.stop)
        handle_error = MagicMock()
        srv.server.handle_error = handle_error
        with patch("http.server.BaseHTTPRequestHandler.handle_one_request",
                   side_effect=ConnectionResetError), \
                patch("traceback.print_exc") as printed:
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            try:
                sock.sendall(b"GET /v1/sessions HTTP/1.1\r\nHost: x\r\n\r\n")
                try:
                    self.assertEqual(sock.recv(4096), b"")
                except ConnectionResetError:
                    pass  # closed with our request unread: also no answer
            finally:
                sock.close()
        printed.assert_not_called()
        handle_error.assert_not_called()

    def test_a_bug_after_the_headers_sends_no_second_answer(self):
        """Once a status line is out, a later failure closes the
        connection instead of appending a 500 to the response."""
        srv = _Server()
        self.addCleanup(srv.stop)
        cred = srv.pair()

        def half(handler, status, payload):
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            raise RuntimeError("after headers")

        with patch.object(hub_mod._Handler, "_send_json", half), \
                patch("traceback.print_exc"):
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
            try:
                sock.sendall(
                    b"GET /v1/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Authorization: Bearer " + cred["credential"].encode()
                    + b"\r\n\r\n")
                data = b""
                while chunk := sock.recv(4096):
                    data += chunk
            finally:
                sock.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:40])
        self.assertEqual(data.count(b"HTTP/1.1 "), 1)

    def test_a_page_bug_answers_500_page_failed(self):
        srv = _Server()
        self.addCleanup(srv.stop)
        cred = srv.pair()
        broken = type("Page", (), {"get": staticmethod(
            lambda ctx, args: {}["missing"])})
        with patch("swe.webapp.pages.load", return_value=broken), \
                patch("traceback.print_exc"):
            status, data = srv.request(
                "GET", "/v1/app/skills",
                headers={"Authorization": f"Bearer {cred['credential']}"})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(data)["error"]["code"], "page_failed")

    def test_inbox_shows_pending_approval_on_active(self):
        # A pane-only prompt leaves the transcript "active" — the inbox
        # must still surface it, and drop it once resolved.
        srv = _Server(sessions=[_session(sid="a1")],
                      statuses=lambda ss: ["active"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        hdr = {"Authorization": f"Bearer {cred['credential']}"}
        srv.state.emit("claude:a1", protocol.event(
            "approval.requested", "claude:a1", "mbp",
            {"request_id": "req-1", "prompt": "run it?"}))
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        items = json.loads(data)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "approval")
        self.assertEqual(items[0]["request_id"], "req-1")
        srv.state.emit("claude:a1", protocol.event(
            "approval.resolved", "claude:a1", "mbp",
            {"request_id": "req-1"}))
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        self.assertEqual(json.loads(data)["items"], [])

    def test_app_sessions_marks_pending_approval(self):
        # The web app has no session inbox; a row waiting on an answer
        # reads "approval" in the Sessions list instead.
        srv = _Server(sessions=[_session(sid="a5")],
                      statuses=lambda ss: ["active"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        hdr = {"Authorization": f"Bearer {cred['credential']}"}
        srv.state.emit("claude:a5", protocol.event(
            "approval.requested", "claude:a5", "mbp",
            {"request_id": "req-5", "prompt": "run it?"}))
        status, data = srv.request("GET", "/v1/app/sessions", headers=hdr)
        row = next(r for r in json.loads(data)["sessions"]
                   if r["key"] == "claude:a5")
        self.assertEqual(row["status"], "approval")
        srv.state.emit("claude:a5", protocol.event(
            "approval.resolved", "claude:a5", "mbp",
            {"request_id": "req-5"}))
        status, data = srv.request("GET", "/v1/app/sessions", headers=hdr)
        row = next(r for r in json.loads(data)["sessions"]
                   if r["key"] == "claude:a5")
        self.assertEqual(row["status"], "active")

    def test_inbox_old_approval_never_cut_or_dismissed(self):
        # A pending approval blocks its agent, so neither the age cutoff
        # nor a dismissal may hide it.
        old = time.time() - 30 * 86_400
        srv = _Server(sessions=[_session(sid="a2", ts=old)],
                      statuses=lambda ss: ["followup"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        hdr = {"Authorization": f"Bearer {cred['credential']}"}
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        self.assertEqual(json.loads(data)["items"], [])
        srv.state.emit("claude:a2", protocol.event(
            "approval.requested", "claude:a2", "mbp",
            {"request_id": "req-2", "prompt": "run it?"}))
        status, data = srv.request(
            "POST", "/v1/inbox/dismiss", headers=hdr,
            body={"key": "claude:a2", "last_active_at": old})
        self.assertEqual(status, 200, data)  # the dismissal itself took
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        items = json.loads(data)["items"]
        self.assertEqual([i["status"] for i in items], ["approval"])

    def test_inbox_drops_expired_phantom_approval(self):
        # A satellite that died mid-prompt can never emit
        # approval.resolved — the entry expires on its TTL instead of
        # pinning the inbox row forever.
        srv = _Server(sessions=[_session(sid="a3")],
                      statuses=lambda ss: ["active"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        hdr = {"Authorization": f"Bearer {cred['credential']}"}
        srv.state.emit("claude:a3", protocol.event(
            "approval.requested", "claude:a3", "mbp",
            {"request_id": "req-3", "prompt": "run it?"}))
        srv.state._approval_at["claude:a3"] = {
            "req-3": time.time() - APPROVAL_TTL_S - 1}
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        self.assertEqual(json.loads(data)["items"], [])
        self.assertNotIn("claude:a3", srv.state.pending_approvals)
        self.assertNotIn("claude:a3", srv.state._approval_at)

    def test_expiry_drops_only_the_stale_request(self):
        # One prompt past its TTL, one fresh, same session: the sweep
        # must keep the fresh one in both maps and in the inbox.
        srv = _Server(sessions=[_session(sid="a7")],
                      statuses=lambda ss: ["active"])
        self.addCleanup(srv.stop)
        hdr = {"Authorization": f"Bearer {srv.pair()['credential']}"}
        key = "claude:a7"
        for rid in ("req-old", "req-new"):
            srv.state.emit(key, protocol.event(
                "approval.requested", key, "mbp",
                {"request_id": rid, "prompt": "run it?"}))
        srv.state._approval_at[key]["req-old"] = (
            time.time() - APPROVAL_TTL_S - 1)
        status, data = srv.request("GET", "/v1/inbox", headers=hdr)
        items = json.loads(data)["items"]
        self.assertEqual([i["request_id"] for i in items], ["req-new"])
        self.assertEqual(srv.state.pending_approvals[key], "req-new")
        self.assertEqual(set(srv.state._approval_at[key]), {"req-new"})

    def test_overlapping_approvals_resolve_one_at_a_time(self):
        # A Codex app-server can hold two prompts on one session:
        # answering A must leave B pending, not clear the session.
        srv = _Server(sessions=[_session(sid="a6")],
                      statuses=lambda ss: ["active"])
        self.addCleanup(srv.stop)
        key = "claude:a6"
        for rid in ("req-A", "req-B"):
            srv.state.emit(key, protocol.event(
                "approval.requested", key, "mbp",
                {"request_id": rid, "prompt": "run it?"}))
        self.assertEqual(srv.state.pending_approvals[key], "req-B")
        srv.state.emit(key, protocol.event(
            "approval.resolved", key, "mbp", {"request_id": "req-B"}))
        self.assertEqual(srv.state.pending_approvals[key], "req-A")
        srv.state.emit(key, protocol.event(
            "approval.resolved", key, "mbp", {"request_id": "req-A"}))
        self.assertNotIn(key, srv.state.pending_approvals)
        self.assertNotIn(key, srv.state._approval_at)

    def test_advertise_url_never_wildcard(self):
        from swe.remote import catalog, hub
        with patch.object(
            catalog, "tailscale_ip", return_value="100.64.1.2"
        ):
            self.assertEqual(
                hub._advertise_url("0.0.0.0", 8477),
                "http://100.64.1.2:8477")
        with patch.object(
            catalog, "tailscale_ip", return_value=""
        ), patch("socket.gethostname", return_value="srv.lan"):
            self.assertEqual(
                hub._advertise_url("0.0.0.0", 8477),
                "http://srv.lan:8477")
        self.assertEqual(
            hub._advertise_url("100.64.9.9", 9000),
            "http://100.64.9.9:9000")

    def test_machines_lists_self(self):
        self.srv.state._register_machine()
        status, data = self.srv.request(
            "GET", "/v1/machines", headers=self.hdr
        )
        machines = json.loads(data)["machines"]
        self.assertTrue(
            any(m["id"] == self.srv.state.machine_id for m in machines)
        )

    def test_messages_before_limit(self):
        for i in range(5):
            self.srv.state.emit(
                "claude:s1",
                protocol.event(
                    "message.appended", "claude:s1", "mbp",
                    protocol.message("assistant", f"m{i}"),
                ),
            )
        status, data = self.srv.request(
            "GET", "/v1/sessions/claude:s1/messages?limit=2",
            headers=self.hdr,
        )
        msgs = json.loads(data)["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[-1]["data"]["text"], "m4")
        status, data = self.srv.request(
            "GET", "/v1/sessions/claude:s1/messages",
            headers=self.hdr,
        )
        all_msgs = json.loads(data)["messages"]
        cutoff = all_msgs[2]["seq"]
        status, data = self.srv.request(
            "GET",
            f"/v1/sessions/claude:s1/messages?before={cutoff}",
            headers=self.hdr,
        )
        msgs = json.loads(data)["messages"]
        self.assertEqual(
            [m["data"]["text"] for m in msgs], ["m0", "m1"]
        )

    def test_messages_page_reads_the_journal_tail(self):
        # A page comes from the end of the journal, not a parse of all
        # of it; other event types and has_more/next_before still hold.
        for i in range(5):
            self.srv.state.emit(
                "claude:s1",
                protocol.event(
                    "message.appended", "claude:s1", "mbp",
                    protocol.message("assistant", f"m{i}"),
                ),
            )
            self.srv.state.emit(
                "claude:s1",
                protocol.event("session.status", "claude:s1", "mbp",
                               {"state": "idle"}),
            )
        with patch.object(
            self.srv.state.journal, "read_since",
            side_effect=AssertionError("whole-journal read"),
        ):
            status, data = self.srv.request(
                "GET", "/v1/sessions/claude:s1/messages?limit=2",
                headers=self.hdr,
            )
            self.assertEqual(status, 200)
            body = json.loads(data)
            self.assertEqual(
                [m["data"]["text"] for m in body["messages"]], ["m3", "m4"])
            self.assertTrue(body["has_more"])
            self.assertEqual(body["next_before"], body["messages"][0]["seq"])
            status, data = self.srv.request(
                "GET",
                f"/v1/sessions/claude:s1/messages?limit=3"
                f"&before={body['next_before']}",
                headers=self.hdr,
            )
            body = json.loads(data)
            self.assertEqual(
                [m["data"]["text"] for m in body["messages"]],
                ["m0", "m1", "m2"])
            self.assertFalse(body["has_more"])
            self.assertIsNone(body["next_before"])

    def test_events_unknown_key_404(self):
        status, _ = self.srv.request(
            "GET", "/v1/sessions/ghost/events", headers=self.hdr
        )
        self.assertEqual(status, 404)

    def _launch(self, body, installed=True, tracked=True):
        with patch("swe.remote.catalog.launch_command",
                   return_value="claude" if installed else None), \
             patch("swe.remote.catalog.launchable_harnesses",
                   return_value=[]), \
             patch("swe.remote.catalog.untracked",
                   return_value=[] if tracked else [body.get("harness")]), \
             patch("swe.remote.catalog.known_session_ids",
                   return_value=["old"]), \
             patch("swe.remote.supervise.tmux_bin",
                   return_value="/usr/bin/tmux"):
            status, data = self.srv.request(
                "POST", "/v1/sessions", body=body, headers=self.hdr)
            # The launch thread reads these patches after the 202 went
            # out: it has to finish before they are undone.
            self._wait_launch_threads()
        return status, json.loads(data)

    def _wait_launch_threads(self):
        for t in threading.enumerate():
            if t.name.startswith("remote-launch-"):
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), t.name)

    def _launch_view(self, launch_id):
        status, data = self.srv.request(
            "GET", f"/v1/launches/{launch_id}", headers=self.hdr)
        return status, json.loads(data)

    def test_new_session_launch(self):
        # launch_new is mocked: without it this route could spawn a real
        # claude process wherever tmux happens to be installed.
        with patch(
            "swe.remote.supervise.launch_new",
            return_value=(True, "launched", "qv-t"),
        ) as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertEqual(status, 202, body)
        launch_id = body["launch_id"]
        self.assertEqual(body["state"], "pending")
        self.assertEqual(launch.call_args[1]["launch_id"], launch_id)
        self.assertEqual(launch.call_args[1]["binary"], "claude")
        status, view = self._launch_view(launch_id)
        self.assertEqual((status, view["state"]), (200, "pending"))
        # Adoption names the key; the launch reads started once the
        # catalog has that row (claude:s1 is in this server's catalog).
        self.srv.state.emit("_global", protocol.event(
            "session.launched", "claude:s1", "m", {"launch_id": launch_id}))
        _, view = self._launch_view(launch_id)
        self.assertEqual((view["state"], view["key"]), ("started", "claude:s1"))

    def test_a_second_launch_in_a_waiting_folder_is_refused(self):
        # One new session per folder: a launch still waiting to be
        # adopted in /tmp turns the next one away, naming its pane.
        with patch("swe.remote.supervise.fork_blocker",
                   return_value="swe-0123abcd") as blocker, \
             patch("swe.remote.supervise.launch_new") as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "launch_pending")
        self.assertIn("swe-0123abcd", body["error"]["message"])
        self.assertEqual(blocker.call_args[0][1:], ("claude", "/tmp"))
        launch.assert_not_called()
        # The launch itself re-checks (exclusive) under the lock.
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "swe-t")) as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertEqual(status, 202, body)
        self.assertTrue(launch.call_args[1]["exclusive"])

    def test_successful_launch_wakes_the_catalog_loop(self):
        # launch_new's prime can end while the catalog loop sleeps out
        # its 10 s tick; the wake makes adoption look for the transcript
        # at once instead. A client already active, so the request's own
        # low-power wake (note_client) can't stand in for it.
        self.srv.state._client_seen_at = time.monotonic()
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "qv-t")):
            self._launch({"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertTrue(self.srv.state._catalog_wake.is_set())

    def test_failed_launch_leaves_the_catalog_loop_asleep(self):
        # A client already active: its request alone wakes nothing
        # (the first request after low power would, see note_client).
        self.srv.state._client_seen_at = time.monotonic()
        with patch("swe.remote.supervise.launch_new",
                   return_value=(False, "nope", None)):
            self._launch({"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertFalse(self.srv.state._catalog_wake.is_set())

    def test_pending_marker_forces_a_fresh_scan(self):
        # While a launch's marker waits for adoption the aggregator's
        # 60 s disk cache must be bypassed — the new transcript would
        # stay invisible for the rest of its TTL. Invalidating (not
        # use_cache=False) rewrites the cache, so the read after
        # adoption still sees the session.
        from swe.remote import catalog as catalog_mod, supervise

        home = temp_dir(self)
        folder = supervise._pending_dir(home)
        folder.mkdir(parents=True)
        (folder / "qv-x.json").write_text(
            json.dumps({"launched_at": time.time()}))
        cat = catalog_mod.Catalog(home)
        with patch("swe.sessions.aggregator.invalidate_cache") as inv, \
             patch("swe.sessions.aggregator.get_all_sessions",
                   return_value=[]) as get:
            cat.refresh()
        inv.assert_called_once_with()
        get.assert_called_once_with(limit=None, use_cache=True)
        (folder / "qv-x.json").unlink()
        with patch("swe.sessions.aggregator.invalidate_cache") as inv, \
             patch("swe.sessions.aggregator.get_all_sessions",
                   return_value=[]):
            cat.refresh()
        inv.assert_not_called()

    def test_launch_started_waits_for_catalog_row(self):
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "qv-t")):
            _, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.srv.state.emit("claude:new", protocol.event(
            "session.launched", "claude:new", "m",
            {"launch_id": body["launch_id"]}))
        _, view = self._launch_view(body["launch_id"])
        self.assertEqual(view["state"], "pending")

    def test_launch_failure_after_reply_is_remembered(self):
        # launch_new runs after the 202, so its failure lands on the
        # launch the phone is polling.
        with patch(
            "swe.remote.supervise.launch_new",
            return_value=(False, "tmux launch failed", None),
        ):
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"})
        self.assertEqual(status, 202, body)
        _, view = self._launch_view(body["launch_id"])
        self.assertEqual(view["state"], "failed")
        self.assertIn("tmux", view["note"])

    def test_untracked_harness_launch_says_no_chat(self):
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "qv-t")) as launch:
            _, body = self._launch(
                {"harness": "qwen-code", "cwd": "/tmp", "prompt": "hi"},
                tracked=False)
        self.assertEqual(launch.call_args[1]["known_ids"], ["old"])
        _, view = self._launch_view(body["launch_id"])
        self.assertEqual(view["state"], "untracked")
        self.assertIn("no chat", view["note"])

    def test_refresh_launchable_notices_a_renamed_harness(self):
        state = self.srv.state
        items = [{"name": "kilo", "command": "kilo", "display_name": "Kilo",
                  "tracked": True}]
        with patch("swe.remote.catalog.launchable_harnesses",
                   return_value=items):
            state._refresh_launchable()
        items[0]["display_name"] = "Kilo Code"
        with patch("swe.remote.catalog.launchable_harnesses",
                   return_value=items):
            state._refresh_launchable()
        from swe.remote import catalog as catalog_mod
        row = catalog_mod.load_machines(state.home)[state.machine_id]
        self.assertEqual(row["harness_names"], {"kilo": "Kilo Code"})

    def test_missing_binary_is_422_before_launch(self):
        with patch("swe.remote.supervise.launch_new") as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi"},
                installed=False)
        self.assertEqual((status, body["error"]["code"]),
                         (422, "not_launchable"))
        launch.assert_not_called()
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_validation(self):
        with patch("swe.remote.supervise.launch_new") as launch:
            cases = [
                ({"harness": "Not A Name", "cwd": "/tmp", "prompt": "hi"},
                 "bad_command"),
                ({"harness": "claude", "cwd": "tmp", "prompt": "hi"},
                 "bad_command"),
                ({"harness": "claude", "cwd": "/tmp", "prompt": "  "},
                 "bad_command"),
                ({"harness": "claude", "cwd": "/no/such/dir", "prompt": "hi"},
                 "bad_cwd"),
            ]
            for req, code in cases:
                status, body = self._launch(req)
                self.assertEqual((status, body["error"]["code"]),
                                 (422, code), req)
        launch.assert_not_called()
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_carries_model_and_effort(self):
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "qv-t")) as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi",
                 "model": "opus", "effort": "high"})
        self.assertEqual(status, 202, body)
        self.assertEqual(launch.call_args[1]["model"], "opus")
        self.assertEqual(launch.call_args[1]["effort"], "high")
        # The launch record keeps the pick for the picker UI and
        # command.result bookkeeping.
        _, view = self._launch_view(body["launch_id"])
        self.assertEqual(view["model"], "opus")
        self.assertEqual(view["effort"], "high")

    def test_launch_carries_yolo(self):
        with patch("swe.remote.supervise.launch_new",
                   return_value=(True, "launched", "qv-t")) as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi",
                 "yolo": True})
        self.assertEqual(status, 202, body)
        self.assertIs(launch.call_args[1]["yolo"], True)
        _, view = self._launch_view(body["launch_id"])
        self.assertIs(view["yolo"], True)

    def test_launch_refuses_yolo_the_harness_cannot_take(self):
        with patch("swe.remote.supervise.launch_new") as launch:
            status, body = self._launch(
                {"harness": "gemini", "cwd": "/tmp", "prompt": "hi",
                 "yolo": True})
        self.assertEqual((status, body["error"]["code"]),
                         (409, "unsupported_launch_option"))
        launch.assert_not_called()
        self.assertEqual(self.srv.state.launches, {})
        with patch("swe.remote.supervise.launch_new") as launch:
            status, body = self._launch(
                {"harness": "claude", "cwd": "/tmp", "prompt": "hi",
                 "yolo": "yes"})
        self.assertEqual((status, body["error"]["code"]),
                         (422, "bad_command"))
        launch.assert_not_called()

    def test_launch_refuses_a_pick_the_harness_cannot_take(self):
        with patch("swe.remote.supervise.launch_new") as launch:
            status, body = self._launch(
                {"harness": "gemini", "cwd": "/tmp", "prompt": "hi",
                 "model": "opus"})
        self.assertEqual((status, body["error"]["code"]),
                         (409, "unsupported_launch_option"))
        launch.assert_not_called()
        self.assertEqual(self.srv.state.launches, {})

    def test_launch_rejects_a_bad_model_shape(self):
        with patch("swe.remote.supervise.launch_new") as launch:
            for bad in ({"model": 3}, {"model": ["opus"]},
                        {"model": "two words"}, {"effort": {"x": 1}}):
                status, body = self._launch(
                    {"harness": "claude", "cwd": "/tmp", "prompt": "hi",
                     **bad})
                self.assertEqual(
                    (status, body["error"]["code"]),
                    (422, "bad_command"), bad)
        launch.assert_not_called()
        self.assertEqual(self.srv.state.launches, {})

    def _attach_srv(self):
        srv = _Server(
            sessions=[_session(sid="ao")], statuses=lambda ss: ["done"])
        self.addCleanup(srv.stop)
        cred = srv.pair()
        return srv, {"Authorization": f"Bearer {cred['credential']}"}

    def test_attach_carries_model_and_effort(self):
        # The pick's channel check runs on the resolved attach mode.
        srv, hdr = self._attach_srv()
        with patch("swe.remote.supervise.attach",
                   return_value=(True, "attached")) as att, \
             patch("swe.remote.supervise.resolve_mode",
                   return_value="headless"):
            status, body = srv.request(
                "POST", "/v1/sessions/claude:ao/control",
                {"op": "attach", "mode": "headless", "model": "opus"},
                headers=hdr)
        self.assertEqual(status, 200, body)
        self.assertEqual(att.call_args[1]["model"], "opus")
        self.assertIsNone(att.call_args[1]["effort"])

    def test_attach_refuses_a_pick_the_channel_cannot_take(self):
        srv, hdr = self._attach_srv()
        with patch("swe.remote.supervise.attach") as att, \
             patch("swe.remote.supervise.resolve_mode",
                   return_value="headless"):
            status, body = srv.request(
                "POST", "/v1/sessions/claude:ao/control",
                {"op": "attach", "mode": "headless", "effort": "high"},
                headers=hdr)
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "unsupported_launch_option")
        att.assert_not_called()

    def test_attach_rejects_a_bad_model_shape(self):
        srv, hdr = self._attach_srv()
        with patch("swe.remote.supervise.attach") as att, \
             patch("swe.remote.supervise.resolve_mode",
                   return_value="headless"):
            status, body = srv.request(
                "POST", "/v1/sessions/claude:ao/control",
                {"op": "attach", "mode": "headless", "model": 5},
                headers=hdr)
        self.assertEqual(status, 422, body)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_command")
        att.assert_not_called()

    def test_unknown_launch_404(self):
        status, _ = self._launch_view("lnope")
        self.assertEqual(status, 404)

    def test_launches_require_auth(self):
        status, _ = self.srv.request("GET", "/v1/launches/lx")
        self.assertEqual(status, 401)

    def test_old_launches_are_pruned(self):
        old = self.srv.state.new_launch("m", "claude", "/tmp")
        self.srv.state.launches[old]["created_at"] -= 2 * 86400
        self.srv.state.new_launch("m", "claude", "/tmp")
        self.assertNotIn(old, self.srv.state.launches)

    def test_adoption_leaves_the_sessions_journal_empty(self):
        # The tailer skips history when the session already has a
        # journal, so session.launched must not land there first.
        lid = self.srv.state.new_launch("m", "claude", "/tmp")
        with patch("swe.remote.supervise.adopt_pending",
                   return_value=["claude:new"]), \
             patch("swe.remote.supervise.meta_for",
                   return_value={"launch_id": lid}):
            self.srv.state._adopt_launches()
        self.assertEqual(self.srv.state.journal.last_seq("claude:new"), 0)
        self.assertEqual(self.srv.state.launches[lid]["key"], "claude:new")
        # A launched or forked session starts mid-turn: tail it hot.
        self.assertTrue(self.srv.state.tail_manager.is_hot("claude:new"))

    def test_folders_lists_subfolders_only(self):
        root = temp_dir(self)
        for name in ("beta", "Alpha", ".hidden"):
            (root / name).mkdir()
        (root / "notes.txt").write_text("x")
        status, data = self.srv.request(
            "GET", f"/v1/folders?path={root}", headers=self.hdr)
        body = json.loads(data)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["path"], os.path.realpath(root))
        self.assertEqual([f["name"] for f in body["folders"]],
                         ["Alpha", "beta"])
        self.assertEqual(body["parent"],
                         os.path.dirname(os.path.realpath(root)))
        self.assertFalse(body["truncated"])

    def test_folders_defaults_home_and_refuses_bad_paths(self):
        home = temp_dir(self)
        with patch.dict(os.environ, {"HOME": str(home)}):
            status, data = self.srv.request(
                "GET", "/v1/folders", headers=self.hdr)
        self.assertEqual(json.loads(data)["path"], os.path.realpath(home))
        for query, code in (("path=relative", "bad_cwd"),
                            ("path=/no/such/dir", "bad_cwd"),
                            ("machine=other", "browse_unavailable")):
            status, data = self.srv.request(
                "GET", f"/v1/folders?{query}", headers=self.hdr)
            self.assertEqual((status, json.loads(data)["error"]["code"]),
                             (422, code), query)

    def test_folders_root_has_no_parent_and_needs_auth(self):
        status, data = self.srv.request(
            "GET", "/v1/folders?path=/", headers=self.hdr)
        self.assertIsNone(json.loads(data)["parent"])
        status, _ = self.srv.request("GET", "/v1/folders?path=/")
        self.assertEqual(status, 401)

    def test_settled_launch_stays_settled(self):
        lid = self.srv.state.new_launch("m", "claude", "/tmp")
        self.srv.state.settle_launch(lid, state="started", key="claude:s1")
        self.srv.state.settle_launch(lid, state="failed", note="late")
        self.assertEqual(self.srv.state.launches[lid]["state"], "started")

    def test_static_assets(self):
        """The web app is the root: ``/`` is its page and its assets sit
        beside it. The old ``/app/`` paths, and a bookmarked
        /index.html, redirect to the same name at the root, so pair links
        minted before #944 keep working (the fragment rides along)."""
        for path in ("/", "/app.js", "/styles.css", "/harness-devin.svg",
                     "/icon-192.png", "/icon-512.png"):
            status, _ = self.srv.request("GET", path)
            self.assertEqual(status, 200, path)
        for path, where in (("/app/", "/"), ("/app", "/"), ("/index.html", "/"),
                            ("/app/app.js", "/app.js"),
                            ("/app/pages.css", "/pages.css"),
                            ("/app/?x=1&y=2", "/?x=1&y=2"),
                            ("/app/app.js?v=3", "/app.js?v=3"),
                            ("/index.html?x=1", "/?x=1")):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.srv.port, timeout=5)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 301, path)
                self.assertEqual(resp.getheader("Location"), where, path)
            finally:
                conn.close()
        # A legacy path redirects only to a name the root serves: one that
        # would put another host in Location is a 404, not a redirect.
        for path in ("/app//evil.example/", "/app//evil.example", "/app/x/y.js",
                     "/app/nope.js", "/app/%2e%2e/hub.py"):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.srv.port, timeout=5)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 404, path)
                self.assertIsNone(resp.getheader("Location"), path)
            finally:
                conn.close()
        # The remote PWA's /pair is gone, and nothing but a known asset
        # is served at the root.
        for path in ("/pair", "/nope.js", "/hub.py", "/pages/chat.js"):
            status, _ = self.srv.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_every_icon_the_page_names_is_served(self):
        """The page's favicons and touch icon, and each manifest icon,
        resolve at the root with their own content type; the maskable
        one is marked so."""
        status, page = self.srv.request("GET", "/")
        self.assertEqual(status, 200)
        status, manifest = self.srv.request("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        icons = json.loads(manifest)["icons"]
        self.assertEqual({i["src"]: i["purpose"] for i in icons}, {
            "/icon-192.png": "any", "/icon-512.png": "any",
            "/icon-maskable-512.png": "maskable"})
        types = {".ico": "image/x-icon", ".png": "image/png",
                 ".svg": "image/svg+xml",
                 ".webmanifest": "application/manifest+json"}
        for path in ("/favicon.ico", "/favicon-32.png", "/favicon.svg",
                     "/apple-touch-icon.png", "/manifest.webmanifest",
                     *(i["src"] for i in icons)):
            self.assertIn(f'"{path}"'.encode(), page + manifest, path)
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.srv.port, timeout=5)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 200, path)
                self.assertEqual(resp.getheader("Content-Type"),
                                 types[Path(path).suffix], path)
            finally:
                conn.close()

    def test_root_mount_leaves_the_api_alone(self):
        """The app at ``/`` is no catch-all: every root endpoint the hub
        has (all under ``/v1/``) still answers as API JSON, and an
        unknown ``/v1/`` path is the API's 404, never the app's page."""
        status, body = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # Routes that read only the test server's own state: the
        # /v1/app/* pages scan the real home's registry and sessions.
        for path in ("/v1/sessions", "/v1/inbox", "/v1/devices/me"):
            status, body = self.srv.request("GET", path, headers=self.hdr)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(json.loads(body), dict, path)
        for path in ("/v1/nope", "/v1/app.js", "/v1/", "/v1/app/nope"):
            status, body = self.srv.request("GET", path, headers=self.hdr)
            self.assertEqual(status, 404, path)
            self.assertIn("error", json.loads(body), path)

    def _headers(self, path, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.getheaders()
        finally:
            conn.close()

    def test_every_response_refuses_framing_and_sniffing(self):
        """The app shell, its data, an error and a redirect all carry the
        same policy: no foreign frame, no sniffing, no referrer."""
        for path, headers in (("/", {}), ("/v1/sessions", self.hdr),
                              ("/v1/sessions", {}), ("/app/", {})):
            status, pairs = self._headers(path, headers)
            names = [k.lower() for k, _ in pairs]
            got = dict((k.lower(), v) for k, v in pairs)
            for name in ("content-security-policy", "x-frame-options",
                         "x-content-type-options", "referrer-policy"):
                self.assertEqual(names.count(name), 1, (path, status, name))
            self.assertIn("frame-ancestors 'self'",
                          got["content-security-policy"], path)
            self.assertEqual(got["x-frame-options"], "SAMEORIGIN", path)
            self.assertEqual(got["x-content-type-options"], "nosniff", path)
            self.assertEqual(got["referrer-policy"], "no-referrer", path)

    def test_server_header_names_no_python(self):
        _, pairs = self._headers("/")
        server = dict((k.lower(), v) for k, v in pairs)["server"]
        self.assertEqual(server, "swe-remote")

    def test_bad_body_400(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request(
                "POST", "/v1/sessions/claude:s1/control",
                body="{bad", headers=self.hdr,
            )
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()


# What a push shows: allow/deny, or devin's one-shot yes and its no.
_BUTTONS = (("allow", "Allow"), ("deny", "Deny"))
_DEVIN_BUTTONS = (("1", "Yes  (Approve once)"), ("8", "No"))


class TestScopedApprovalToken(unittest.TestCase):
    """The ntfy action-button bearer: scoped, single-use, approval-only.

    A leaked token answers exactly the prompt it was minted for; the
    paired device credential keeps working beside it for app clients.
    """

    def setUp(self):
        self.srv = _AttachedServer()
        self.addCleanup(self.srv.stop)
        self.tokens = self.srv.state.approval_tokens

    def _post_approval(self, body, token):
        return self.srv.request(
            "POST", "/v1/sessions/claude:s1/approval",
            body=body, headers={"Authorization": f"Bearer {token}"},
        )

    def test_scoped_token_answers_approval(self):
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        status, data = self._post_approval(
            {"request_id": "r1", "decision": "allow"}, tok)
        # Auth passed; headless claude just can't take the decision.
        self.assertEqual(status, 409, data)
        self.assertEqual(
            json.loads(data)["error"]["code"], "approval_not_supported"
        )

    def test_scoped_token_is_single_use(self):
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        body = {"request_id": "r1", "decision": "allow"}
        first, _ = self._post_approval(body, tok)
        self.assertNotEqual(first, 401)
        second, _ = self._post_approval(body, tok)
        self.assertEqual(second, 401)

    def test_scoped_token_can_pick_an_offered_menu_row(self):
        # Option buttons send the row's number — a digit the push
        # offered answers the prompt the token was minted for.
        for decision in ("1", "8"):
            tok = self.tokens.mint("claude:s1", "r1", _DEVIN_BUTTONS)
            status, data = self._post_approval(
                {"request_id": "r1", "decision": decision}, tok)
            self.assertEqual(status, 409, (decision, data))

    def _assert_refused(self, decision, tok):
        with patch("sys.stderr", io.StringIO()) as err:
            status, data = self._post_approval(
                {"request_id": "r1", "decision": decision}, tok)
        self.assertEqual(status, 403, (decision, data))
        self.assertEqual(json.loads(data)["error"]["code"],
                         "decision_not_allowed")
        self.assertIn("refused an ntfy action token", err.getvalue())
        # The refusal burned the token: the offered choice is gone too.
        status, _ = self._post_approval(
            {"request_id": "r1", "decision": "1"}, tok)
        self.assertEqual(status, 401, decision)

    def test_scoped_token_cannot_pick_an_unoffered_row(self):
        # #1464: the push showed rows 1 and 8; devin's bypass sits at
        # row 5. A topic reader holding the token cannot reach it, nor
        # any other unoffered answer or malformed decision.
        for decision in ("5", "2", "3", "0", "123", "always", "allow",
                         3, None, ["1"]):
            tok = self.tokens.mint("claude:s1", "r1", _DEVIN_BUTTONS)
            self._assert_refused(decision, tok)

    def test_scoped_token_cannot_pick_a_standing_row_even_if_offered(self):
        # Even a push that (wrongly) offered every row gets a token that
        # cannot spend always-allow or bypass rows.
        full = list(_DEVIN_BUTTONS) + [
            ("3", "Yes, always allow `git` commands in `repo`"),
            ("5", "Yes, switch to bypass mode"),
        ]
        for decision in ("3", "5"):
            tok = self.tokens.mint("claude:s1", "r1", full)
            self._assert_refused(decision, tok)
        tok = self.tokens.mint(
            "claude:s1", "r1", list(_BUTTONS) + [("always", "Always")])
        with patch("sys.stderr", io.StringIO()):
            status, _ = self._post_approval(
                {"request_id": "r1", "decision": "always"}, tok)
        self.assertEqual(status, 403)

    def test_token_without_choices_is_refused(self):
        # A record in the pre-#1464 shape carries no choices: it spends
        # nothing rather than any row.
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        del self.tokens._tokens[tok]["choices"]
        with patch("sys.stderr", io.StringIO()):
            status, _ = self._post_approval(
                {"request_id": "r1", "decision": "allow"}, tok)
        self.assertEqual(status, 403)

    def test_paired_device_still_answers_any_row(self):
        # The bypass row stays reachable from a paired device: the
        # route authenticates and goes on to the injector (headless
        # claude then refuses the decision itself).
        cred = self.srv.pair()["credential"]
        for decision in ("5", "always", "allow"):
            status, data = self._post_approval(
                {"request_id": "r1", "decision": decision}, cred)
            self.assertEqual(status, 409, (decision, data))

    def test_scoped_token_bound_to_request(self):
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        status, _ = self._post_approval(
            {"request_id": "r2", "decision": "allow"}, tok)
        self.assertEqual(status, 401)

    def test_scoped_token_bound_to_session(self):
        tok = self.tokens.mint("claude:other", "r1", _BUTTONS)
        status, _ = self._post_approval(
            {"request_id": "r1", "decision": "allow"}, tok)
        self.assertEqual(status, 401)

    def test_scoped_token_expires(self):
        past = time.time() - self.tokens.TTL_S - 1
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS, now=past)
        status, _ = self._post_approval(
            {"request_id": "r1", "decision": "allow"}, tok)
        self.assertEqual(status, 401)

    def test_scoped_token_opens_no_other_route(self):
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        for method, path, body in (
            ("POST", "/v1/sessions/claude:s1/message", {"text": "hi"}),
            ("POST", "/v1/sessions/claude:s1/control",
             {"op": "interrupt"}),
            ("GET", "/v1/sessions", None),
        ):
            status, _ = self.srv.request(
                method, path, body=body,
                headers={"Authorization": f"Bearer {tok}"},
            )
            self.assertEqual(status, 401, (method, path))

    def test_resolved_approval_retires_token(self):
        tok = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        self.srv.state.emit(
            "claude:s1",
            protocol.event(
                "approval.resolved", "claude:s1", "m1",
                {"request_id": "r1", "decision": "allow"},
            ),
        )
        status, _ = self._post_approval(
            {"request_id": "r1", "decision": "allow"}, tok)
        self.assertEqual(status, 401)

    def test_resolved_approval_keeps_other_requests_tokens(self):
        # Overlapping approvals on one session: resolving r1 retires
        # r1's token only — r2's buttons must keep working.
        dead = self.tokens.mint("claude:s1", "r1", _BUTTONS)
        live = self.tokens.mint("claude:s1", "r2", _BUTTONS)
        self.srv.state.emit(
            "claude:s1",
            protocol.event(
                "approval.resolved", "claude:s1", "m1",
                {"request_id": "r1", "decision": "allow"},
            ),
        )
        status, _ = self._post_approval(
            {"request_id": "r1", "decision": "allow"}, dead)
        self.assertEqual(status, 401)
        status, data = self._post_approval(
            {"request_id": "r2", "decision": "allow"}, live)
        self.assertNotEqual(status, 401, data)


class TestInboxCutoff(unittest.TestCase):
    """catalog.inbox(): the age cutoff, dismissals and finished-today."""

    NOW = 1_800_000_000.0

    def _catalog(self, rows):
        from swe.remote.catalog import Catalog
        home = temp_dir(self)
        sessions = [_session(sid=sid, ts=ts) for sid, ts, _st in rows]
        cat = Catalog(home, machine_id="mbp",
                      fetch_sessions=lambda: sessions,
                      status_of=lambda ss: [st for _s, _t, st in rows])
        cat.refresh()
        return cat

    def keys(self, items):
        return sorted(i["key"] for i in items)

    def test_cutoff_drops_old_followups_errors_interrupts(self):
        from swe.remote.catalog import INBOX_MAX_AGE_S
        fresh = self.NOW - INBOX_MAX_AGE_S + 60
        stale = self.NOW - INBOX_MAX_AGE_S - 60
        cat = self._catalog([
            ("f-new", fresh, "followup"), ("f-old", stale, "followup"),
            ("e-new", fresh, "error"), ("e-old", stale, "error"),
            ("i-new", fresh, "interrupted"), ("i-old", stale, "interrupted"),
            ("active", fresh, "active"),
        ])
        self.assertEqual(
            self.keys(cat.inbox(now=self.NOW)),
            ["claude:e-new", "claude:f-new", "claude:i-new"],
        )

    def test_approval_never_cut(self):
        cat = self._catalog([("a", 0, "active")])
        items = cat.inbox(pending={"claude:a": "r1"}, now=self.NOW)
        self.assertEqual(
            [(i["status"], i["request_id"]) for i in items],
            [("approval", "r1")],
        )

    def test_dismissal_hides_until_new_activity(self):
        t = self.NOW - 3600
        cat = self._catalog([("f", t, "followup")])
        self.assertEqual(
            cat.inbox(dismissed={"claude:f": t}, now=self.NOW), [])
        self.assertEqual(
            cat.inbox(dismissed={"claude:f": t + 1}, now=self.NOW), [])
        self.assertEqual(
            self.keys(cat.inbox(dismissed={"claude:f": t - 1},
                                now=self.NOW)),
            ["claude:f"],
        )

    def test_done_today_is_opt_in(self):
        from swe.remote.catalog import _local_midnight
        midnight = _local_midnight(self.NOW)
        cat = self._catalog([
            ("today", midnight + 1, "done"),
            ("yesterday", midnight - 1, "done"),
        ])
        self.assertEqual(cat.inbox(now=self.NOW), [])
        items = cat.inbox(include_done=True, now=self.NOW)
        self.assertEqual([(i["key"], i["status"]) for i in items],
                         [("claude:today", "done")])
        self.assertEqual(
            cat.inbox(include_done=True, now=self.NOW,
                      dismissed={"claude:today": midnight + 1}),
            [],
        )


class TestInboxDismiss(unittest.TestCase):
    """POST /v1/inbox/dismiss: auth, per-device storage, reappearing."""

    def setUp(self):
        self.t = time.time() - 3600
        self.srv = _Server(sessions=[_session(sid="f1", ts=self.t)],
                           statuses=lambda ss: ["followup"])
        self.addCleanup(self.srv.stop)

    def hdr(self, cred):
        return {"Authorization": f"Bearer {cred['credential']}"}

    def inbox_keys(self, cred):
        status, data = self.srv.request("GET", "/v1/inbox",
                                        headers=self.hdr(cred))
        self.assertEqual(status, 200)
        return [i["key"] for i in json.loads(data)["items"]]

    def dismiss(self, cred, body):
        return self.srv.request("POST", "/v1/inbox/dismiss", body=body,
                                headers=self.hdr(cred) if cred else {})

    def test_requires_auth(self):
        status, _ = self.dismiss(
            None, {"key": "claude:f1", "last_active_at": self.t})
        self.assertEqual(status, 401)
        cred = self.srv.pair()
        pairing.revoke_device(self.srv.home, cred["device_id"])
        status, _ = self.dismiss(
            cred, {"key": "claude:f1", "last_active_at": self.t})
        self.assertEqual(status, 401)

    def test_rejects_bad_body(self):
        cred = self.srv.pair()
        for body in ({}, {"key": "claude:f1"},
                     {"key": "", "last_active_at": 1.0},
                     {"key": "claude:f1", "last_active_at": "1"},
                     {"key": "claude:f1", "last_active_at": True},
                     {"key": 5, "last_active_at": 1.0},
                     {"key": "claude:f1", "last_active_at": 10 ** 400}):
            status, data = self.dismiss(cred, body)
            self.assertEqual(status, 422, body)
            self.assertEqual(json.loads(data)["error"]["code"], "bad_dismiss")

    def test_dismiss_is_per_device_and_persists(self):
        from swe.remote import dismissals
        phone, tablet = self.srv.pair(), self.srv.pair()
        self.assertEqual(self.inbox_keys(phone), ["claude:f1"])
        status, data = self.dismiss(
            phone, {"key": "claude:f1", "last_active_at": self.t})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.inbox_keys(phone), [])
        self.assertEqual(self.inbox_keys(tablet), ["claude:f1"])
        path = dismissals.dismissals_file(self.srv.home)
        self.assertEqual(path.parent, pairing.devices_file(
            self.srv.home).parent)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Read straight off disk: what a restarted hub would see.
        self.assertEqual(
            dismissals.for_device(self.srv.home, phone["device_id"]),
            {"claude:f1": self.t},
        )

    def test_new_activity_brings_item_back(self):
        cred = self.srv.pair()
        self.dismiss(cred, {"key": "claude:f1", "last_active_at": self.t})
        self.assertEqual(self.inbox_keys(cred), [])
        later = [_session(sid="f1", ts=self.t + 60)]
        self.srv.state.catalog._fetch = lambda: later
        self.srv.state.catalog.refresh()
        self.assertEqual(self.inbox_keys(cred), ["claude:f1"])

    def test_store_prunes_by_dismissal_time(self):
        from swe.remote import dismissals
        home = self.srv.home
        now = time.time()
        dismissals.dismiss(home, "d1", "claude:old", 5.0,
                           now=now - dismissals.KEEP_S - 1)
        dismissals.dismiss(home, "d1", "claude:new", 5.0, now=now)
        self.assertEqual(dismissals.for_device(home, "d1", now=now),
                         {"claude:new": 5.0})
        raw = json.loads(dismissals.dismissals_file(home).read_text())
        self.assertEqual(list(raw["devices"]["d1"]), ["claude:new"])

    def test_write_prunes_every_device(self):
        # An idle (or revoked) device's expired rows go on any write.
        from swe.remote import dismissals
        home = self.srv.home
        now = time.time()
        dismissals.dismiss(home, "idle", "k", 5.0,
                           now=now - dismissals.KEEP_S - 1)
        dismissals.dismiss(home, "busy", "k", 5.0, now=now)
        raw = json.loads(dismissals.dismissals_file(home).read_text())
        self.assertEqual(list(raw["devices"]), ["busy"])

    def test_stale_retry_keeps_newer_dismissal(self):
        from swe.remote import dismissals
        home = self.srv.home
        dismissals.dismiss(home, "d1", "k", 20.0)
        dismissals.dismiss(home, "d1", "k", 10.0)
        self.assertEqual(dismissals.for_device(home, "d1"), {"k": 20.0})


class TestSendQueue(unittest.TestCase):
    """POST message → 202, then the send runs on the per-key worker."""

    def setUp(self):
        self.srv = _AttachedServer()
        self.addCleanup(self.srv.stop)
        cred = self.srv.pair()
        self.hdr = {"Authorization": f"Bearer {cred['credential']}"}
        self.key = "claude:s1"
        # Fresh-status reads stub out to this box; tests flip it.
        self.live = ["done"]
        self.srv.state.status_of = lambda sessions: list(self.live)

    def _post(self, body):
        return self.srv.request(
            "POST", f"/v1/sessions/{self.key}/message",
            body=body, headers=self.hdr,
        )

    def _injector(self):
        from swe.remote import inject
        fake = MagicMock()
        fake.send.return_value = inject.InjectResult(True, "sent")
        return fake

    def test_message_202_then_result_event(self):
        from swe.remote import inject
        fake = self._injector()
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake):
            status, data = self._post(
                {"text": "hi", "client_id": "cid-1"}
            )
            self.assertEqual(status, 202, data)
            body = json.loads(data)
            ev = q.get(timeout=5)
        self.assertTrue(body["ok"])
        self.assertTrue(body["queued"])
        self.assertEqual(body["client_id"], "cid-1")
        self.assertFalse(body["waiting"])
        self.assertEqual(ev["type"], "command.result")
        self.assertTrue(ev["data"]["ok"])
        self.assertEqual(ev["data"]["op"], "message")
        self.assertEqual(ev["data"]["client_id"], "cid-1")
        # Same event is durable: replaying the journal sees it.
        evs, _ = self.srv.state.journal.read_since(self.key, 0)
        self.assertEqual(evs[-1]["type"], "command.result")

    def test_send_wakes_the_session_tailer(self):
        from swe.remote import inject
        fake = self._injector()
        mgr = self.srv.state.tail_manager
        self.assertFalse(mgr.is_hot(self.key))
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake):
            status, data = self._post({"text": "hi", "mode": "steer"})
            self.assertEqual(status, 202, data)
            q.get(timeout=5)
        fake.send.assert_called_once()
        self.assertTrue(mgr.is_hot(self.key))
        # The tail loop is told to poll it now, not on its next tick.
        self.assertIn(self.key, mgr._woken)
        self.assertTrue(mgr.wait(0))

    def test_queue_waits_for_idle(self):
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        polled = threading.Event()
        flip = threading.Event()

        def status_of(sessions):
            polled.set()
            return ["done" if flip.is_set() else "active"]

        self.srv.state.status_of = status_of
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05):
            status, data = self._post({"text": "hi"})
            self.assertEqual(status, 202, data)
            self.assertTrue(json.loads(data)["waiting"])
            self.assertTrue(polled.wait(2))
            self.assertFalse(fake.send.called)
            flip.set()
            ev = q.get(timeout=5)
        self.assertTrue(ev["data"]["ok"])
        fake.send.assert_called_once()
        # The injector saw the fresh status, not the stale row's.
        self.assertEqual(fake.send.call_args[0][2], "done")

    def test_queue_timeout_reports_failure(self):
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        self.live = ["active"]
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_WAIT_MAX_S", 0.1), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.02):
            status, _ = self._post({"text": "hi"})
            self.assertEqual(status, 202)
            ev = q.get(timeout=5)
        self.assertFalse(ev["data"]["ok"])
        self.assertIn("stayed busy", ev["data"]["note"])
        fake.send.assert_not_called()

    def test_steer_sends_immediately_while_active(self):
        from swe.remote import inject
        fake = self._injector()
        self.live = ["active"]
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake):
            status, data = self._post({"text": "go", "mode": "steer"})
            self.assertEqual(status, 202, data)
            self.assertFalse(json.loads(data)["waiting"])
            ev = q.get(timeout=5)
        self.assertTrue(ev["data"]["ok"])
        fake.send.assert_called_once()
        self.assertEqual(fake.send.call_args[0][2], "active")

    def test_second_queue_send_rechecks_status_at_run(self):
        # Job 2 submitted while its key still read idle must not inject
        # into the turn job 1 just started: the wait uses a fresh read
        # taken when the job runs, not the submit-time one.
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        job1_in_send = threading.Event()
        release_job1 = threading.Event()
        turn_idle = threading.Event()

        def send(session, cmd, status):
            if cmd["text"] == "one":
                job1_in_send.set()
                release_job1.wait(5)
            return inject.InjectResult(True, "sent")

        fake.send.side_effect = send

        def status_of(sessions):
            busy = job1_in_send.is_set() and not turn_idle.is_set()
            return ["active" if busy else "done"]

        self.srv.state.status_of = status_of
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05):
            self.assertEqual(self._post({"text": "one"})[0], 202)
            self.assertTrue(job1_in_send.wait(2))
            # Job 1 is mid-turn now; queueing a second message reports it.
            status, data = self._post({"text": "two"})
            self.assertEqual(status, 202)
            self.assertTrue(json.loads(data)["waiting"])
            release_job1.set()
            ev1 = q.get(timeout=5)
            self.assertTrue(ev1["data"]["ok"])
            # No result for job 2 while the turn is still hot.
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.5)
            turn_idle.set()
            ev2 = q.get(timeout=5)
        self.assertTrue(ev2["data"]["ok"])
        sent = [c[0] for c in fake.send.call_args_list]
        self.assertEqual([s[1]["text"] for s in sent], ["one", "two"])
        # Job 2 waited for the flip, then sent with the fresh status.
        self.assertEqual(sent[1][2], "done")

    def test_sends_run_fifo_per_key(self):
        from swe.remote import inject
        fake = self._injector()
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake):
            self.assertEqual(self._post({"text": "first"})[0], 202)
            self.assertEqual(self._post({"text": "second"})[0], 202)
            evs = [q.get(timeout=5), q.get(timeout=5)]
        self.assertTrue(all(e["data"]["ok"] for e in evs))
        sent = [c[0][1]["text"] for c in fake.send.call_args_list]
        self.assertEqual(sent, ["first", "second"])

    def test_steer_bypasses_a_waiting_queue_job(self):
        # Steers get their own queue key: one queue job parked on an
        # active session must not hold a steer back.
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        self.live = ["active"]
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05):
            status, data = self._post({"text": "q1"})
            self.assertEqual(status, 202)
            self.assertTrue(json.loads(data)["waiting"])
            status, data = self._post({"text": "s1", "mode": "steer"})
            self.assertEqual(status, 202)
            self.assertFalse(json.loads(data)["waiting"])
            ev = q.get(timeout=5)
            self.assertTrue(ev["data"]["ok"])
            # The queue job is still parked; only the steer has sent.
            self.assertEqual(
                [c[0][1]["text"] for c in fake.send.call_args_list],
                ["s1"],
            )
            self.live = ["done"]
            ev2 = q.get(timeout=5)
        self.assertTrue(ev2["data"]["ok"])
        self.assertEqual(
            [c[0][1]["text"] for c in fake.send.call_args_list],
            ["s1", "q1"],
        )

    def test_job_refetches_session_during_wait(self):
        # The Session captured at submit can go stale mid-wait — each
        # poll and the final send must see the catalog's current one.
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        newer = _session(sid="s1")
        flip = threading.Event()
        job_started = threading.Event()
        seen = []

        def status_of(sessions):
            seen.append(sessions[0])
            if len(seen) >= 2:  # handler's read plus the job's first
                job_started.set()
            return ["done" if flip.is_set() else "active"]

        self.srv.state.status_of = status_of
        real_session_for = self.srv.state.catalog.session_for
        self.srv.state.catalog.session_for = (
            lambda k: newer if flip.is_set() else real_session_for(k)
        )
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05):
            self.assertEqual(self._post({"text": "hi"})[0], 202)
            self.assertTrue(job_started.wait(2))
            flip.set()
            ev = q.get(timeout=5)
        self.assertTrue(ev["data"]["ok"])
        self.assertIs(seen[-1], newer)
        self.assertIs(fake.send.call_args[0][0], newer)

    def test_close_cancels_queued_and_stop_fails_running(self):
        # Restart semantics: queued jobs are cancelled outright; a job
        # already in its wait loop exits on _stop with the same note.
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        calls = [0]
        job_started = threading.Event()

        def status_of(sessions):
            calls[0] += 1
            if calls[0] >= 2:
                job_started.set()
            return ["active"]

        self.srv.state.status_of = status_of
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05):
            self.assertEqual(
                self._post({"text": "one", "client_id": "j1"})[0], 202
            )
            self.assertTrue(job_started.wait(2))
            self.assertEqual(
                self._post({"text": "two", "client_id": "j2"})[0], 202
            )
            self.srv.state.sendq.close()
            cancelled = q.get(timeout=5)
            self.assertEqual(cancelled["data"]["client_id"], "j2")
            self.assertFalse(cancelled["data"]["ok"])
            self.assertEqual(
                cancelled["data"]["note"],
                "hub restarted; message not sent",
            )
            self.srv.state._stop.set()
            stopped = q.get(timeout=5)
        self.assertEqual(stopped["data"]["client_id"], "j1")
        self.assertFalse(stopped["data"]["ok"])
        self.assertEqual(
            stopped["data"]["note"], "hub restarted; message not sent"
        )
        fake.send.assert_not_called()

    def test_send_backlog_cap_429s(self):
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        fake = self._injector()
        self.live = ["active"]
        with patch.object(inject, "injector_for", return_value=fake), \
             patch.object(hub_mod, "QUEUE_POLL_S", 0.05), \
             patch.object(hub_mod, "MAX_SEND_BACKLOG", 3):
            for i in range(3):
                self.assertEqual(self._post({"text": f"m{i}"})[0], 202)
            status, data = self._post({"text": "m3"})
        self.assertEqual(status, 429)
        self.assertEqual(
            json.loads(data)["error"]["code"], "backlog_full"
        )

    def test_injector_exception_fails_job_not_worker(self):
        from swe.remote import inject
        fake = self._injector()
        fake.send.side_effect = [
            RuntimeError("boom"), inject.InjectResult(True, "sent")
        ]
        q = self.srv.state.bus.subscribe(self.key)
        with patch.object(inject, "injector_for", return_value=fake):
            self.assertEqual(self._post({"text": "one"})[0], 202)
            bad = q.get(timeout=5)
            self.assertEqual(self._post({"text": "two"})[0], 202)
            good = q.get(timeout=5)
        self.assertFalse(bad["data"]["ok"])
        self.assertIn("boom", bad["data"]["note"])
        self.assertTrue(good["data"]["ok"])


class TestQueuedSends(unittest.TestCase):
    """GET/POST /v1/sessions/{key}/queue: the phone's queued-send tray."""

    def setUp(self):
        self.srv = _AttachedServer()
        self.addCleanup(self.srv.stop)
        cred = self.srv.pair()
        self.hdr = {"Authorization": f"Bearer {cred['credential']}"}
        self.key = "claude:s1"
        self.live = ["active"]
        self.srv.state.status_of = lambda sessions: list(self.live)
        from swe.remote import hub as hub_mod
        from swe.remote import inject
        self.fake = MagicMock()
        self.fake.send.return_value = inject.InjectResult(True, "sent")
        for p in (
            patch.object(inject, "injector_for", return_value=self.fake),
            patch.object(hub_mod, "QUEUE_POLL_S", 0.05),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.events = self.srv.state.bus.subscribe(self.key)

    def _send(self, text, cid, mode="queue", **extra):
        return self.srv.request(
            "POST", f"/v1/sessions/{self.key}/message",
            body={"text": text, "mode": mode, "client_id": cid, **extra},
            headers=self.hdr,
        )

    def _act(self, cid, op):
        status, data = self.srv.request(
            "POST", f"/v1/sessions/{self.key}/queue",
            body={"client_id": cid, "op": op}, headers=self.hdr,
        )
        return status, json.loads(data)

    def _list(self):
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{self.key}/queue", headers=self.hdr,
        )
        self.assertEqual(status, 200, data)
        return json.loads(data)["sends"]

    def _result(self):
        ev = self.events.get(timeout=5)
        self.assertEqual(ev["type"], "command.result")
        return ev["data"]

    def _no_more_results(self):
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=0.4)

    def test_list_shows_waiting_send_then_drops_it_once_delivered(self):
        from swe.remote import attachments
        staged, err = attachments.stage(
            self.srv.home, self.key, "shot.png", b"\x89PNG\r\n\x1a\n"
        )
        self.assertIsNotNone(staged, err)
        before = time.time()
        status, data = self._send(
            "after this turn", "q-1", attachments=[staged["ref"]]
        )
        self.assertEqual(status, 202, data)
        self.assertTrue(json.loads(data)["waiting"])
        sends = self._list()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["client_id"], "q-1")
        self.assertEqual(sends[0]["text"], "after this turn")
        # Refs as the phone sent them, never the staged file path.
        self.assertEqual(sends[0]["attachments"], [staged["ref"]])
        self.assertGreaterEqual(sends[0]["queued_at"], before)
        self.live = ["done"]
        self.assertTrue(self._result()["ok"])
        self.assertEqual(self._list(), [])
        self.fake.send.assert_called_once()

    def test_list_keeps_fifo_order_and_skips_sends_without_client_id(self):
        self.assertEqual(self._send("one", "q-1")[0], 202)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{self.key}/message",
            body={"text": "anonymous"}, headers=self.hdr,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self._send("two", "q-2")[0], 202)
        self.assertEqual(
            [s["client_id"] for s in self._list()], ["q-1", "q-2"]
        )

    def test_list_unknown_session_404_and_needs_auth(self):
        status, _ = self.srv.request(
            "GET", "/v1/sessions/claude:nope/queue", headers=self.hdr,
        )
        self.assertEqual(status, 404)
        status, _ = self.srv.request(
            "GET", f"/v1/sessions/{self.key}/queue",
        )
        self.assertEqual(status, 401)
        status, _ = self.srv.request(
            "POST", f"/v1/sessions/{self.key}/queue",
            body={"client_id": "q-1", "op": "cancel"},
        )
        self.assertEqual(status, 401)

    def test_steer_sends_a_waiting_send_while_session_is_active(self):
        self.assertEqual(self._send("now please", "q-1")[0], 202)
        status, body = self._act("q-1", "steer")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "client_id": "q-1",
                                "op": "steer"})
        result = self._result()
        self.assertTrue(result["ok"])
        self.assertEqual(result["client_id"], "q-1")
        self.assertNotIn("cancelled", result)
        self.fake.send.assert_called_once()
        _session, cmd, live = self.fake.send.call_args[0]
        self.assertEqual(cmd["mode"], "steer")
        self.assertEqual(cmd["text"], "now please")
        self.assertEqual(live, "active")
        self.assertEqual(self._list(), [])
        # The parked queue job must not send it a second time on idle.
        self.live = ["done"]
        self._no_more_results()
        self.fake.send.assert_called_once()

    def test_steer_queues_behind_earlier_steers(self):
        from swe.remote import inject
        first_in = threading.Event()
        release = threading.Event()

        def send(session, cmd, status):
            if cmd["text"] == "s0":
                first_in.set()
                release.wait(5)
            return inject.InjectResult(True, "sent")

        self.fake.send.side_effect = send
        self.assertEqual(self._send("s0", "s-0", mode="steer")[0], 202)
        self.assertTrue(first_in.wait(2))
        self.assertEqual(self._send("q1", "q-1")[0], 202)
        self.assertEqual(self._act("q-1", "steer")[0], 200)
        self.assertEqual(self._send("s2", "s-2", mode="steer")[0], 202)
        release.set()
        got = [self._result()["client_id"] for _ in range(3)]
        self.assertEqual(got, ["s-0", "q-1", "s-2"])
        self.assertEqual(
            [c[0][1]["text"] for c in self.fake.send.call_args_list],
            ["s0", "q1", "s2"],
        )

    def test_steer_degrades_like_a_normal_steer(self):
        from swe.remote import inject
        self.fake.send.return_value = inject.InjectResult(
            True, "sent (steer queued: headless runs are atomic)",
            degraded=True,
        )
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.assertEqual(self._act("q-1", "steer")[0], 200)
        result = self._result()
        self.assertTrue(result["ok"])
        self.assertTrue(result["degraded"])
        self.assertIn("steer queued", result["note"])

    def test_cancel_removes_send_and_reports_it(self):
        self.assertEqual(self._send("never mind", "q-1")[0], 202)
        self.assertEqual(self._send("keep me", "q-2")[0], 202)
        status, body = self._act("q-1", "cancel")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["op"], "cancel")
        result = self._result()
        self.assertEqual(result["client_id"], "q-1")
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["note"], "removed before sending")
        self.assertEqual(result["op"], "message")
        self.assertEqual(
            [s["client_id"] for s in self._list()], ["q-2"]
        )
        # The journal keeps it, so a reopened app sees the removal.
        evs, _ = self.srv.state.journal.read_since(self.key, 0)
        self.assertTrue(evs[-1]["data"].get("cancelled"))
        self.live = ["done"]
        kept = self._result()
        self.assertEqual(kept["client_id"], "q-2")
        self.assertTrue(kept["ok"])
        self.assertEqual(
            [c[0][1]["text"] for c in self.fake.send.call_args_list],
            ["keep me"],
        )

    def test_unknown_and_finished_ids_fail_cleanly(self):
        status, body = self._act("nope", "cancel")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "unknown_send")
        self.live = ["done"]
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.assertTrue(self._result()["ok"])
        for op in ("cancel", "steer"):
            status, body = self._act("q-1", op)
            self.assertEqual(status, 409, body)
            self.assertEqual(body["error"]["code"], "not_waiting")
            self.assertIn("already went out", body["error"]["message"])
        self.fake.send.assert_called_once()

    def test_acting_twice_is_409(self):
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.assertEqual(self._act("q-1", "cancel")[0], 200)
        status, body = self._act("q-1", "steer")
        self.assertEqual(status, 409)
        self.assertIn("already removed", body["error"]["message"])
        self.assertEqual(self._send("hi", "q-2")[0], 202)
        self.assertEqual(self._act("q-2", "steer")[0], 200)
        status, body = self._act("q-2", "cancel")
        self.assertEqual(status, 409)
        self.assertIn("already sent now", body["error"]["message"])

    def _edit(self, cid, text):
        status, data = self.srv.request(
            "POST", f"/v1/sessions/{self.key}/queue",
            body={"client_id": cid, "op": "edit", "text": text},
            headers=self.hdr,
        )
        return status, json.loads(data)

    def test_edit_changes_listed_and_injected_text_in_place(self):
        self.assertEqual(self._send("frist", "q-1")[0], 202)
        self.assertEqual(self._send("second", "q-2")[0], 202)
        status, body = self._edit("q-1", "first, fixed")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "client_id": "q-1",
                                "op": "edit", "text": "first, fixed"})
        sends = self._list()
        # Same place in the queue, new text.
        self.assertEqual([s["client_id"] for s in sends], ["q-1", "q-2"])
        self.assertEqual(sends[0]["text"], "first, fixed")
        self.live = ["done"]
        self.assertEqual(self._result()["client_id"], "q-1")
        self.assertEqual(self._result()["client_id"], "q-2")
        self.assertEqual(
            [c[0][1]["text"] for c in self.fake.send.call_args_list],
            ["first, fixed", "second"],
        )

    def test_edit_then_steer_sends_the_edited_text(self):
        self.assertEqual(self._send("old", "q-1")[0], 202)
        self.assertEqual(self._edit("q-1", "new")[0], 200)
        self.assertEqual(self._act("q-1", "steer")[0], 200)
        self.assertTrue(self._result()["ok"])
        _s, cmd, _live = self.fake.send.call_args[0]
        self.assertEqual((cmd["text"], cmd["mode"]), ("new", "steer"))

    def test_edit_after_send_out_or_unknown_fails_cleanly(self):
        status, body = self._edit("nope", "x")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "unknown_send")
        self.live = ["done"]
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.assertTrue(self._result()["ok"])
        status, body = self._edit("q-1", "too late")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_waiting")
        self.assertEqual(self.fake.send.call_args[0][1]["text"], "hi")

    def test_edit_needs_real_text(self):
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        for body in (
            {"client_id": "q-1", "op": "edit"},
            {"client_id": "q-1", "op": "edit", "text": "   "},
            {"client_id": "q-1", "op": "edit", "text": ["a"]},
            {"client_id": "q-1", "op": "edit", "text": {}},
        ):
            status, data = self.srv.request(
                "POST", f"/v1/sessions/{self.key}/queue",
                body=body, headers=self.hdr,
            )
            self.assertEqual(status, 422, body)
            self.assertEqual(
                json.loads(data)["error"]["code"], "bad_command"
            )
        self.assertEqual(self._list()[0]["text"], "hi")

    def test_edit_after_job_claims_is_409_and_old_text_goes(self):
        # The job wins: it claimed the send and is injecting.
        from swe.remote import inject
        in_send = threading.Event()
        release = threading.Event()

        def send(session, cmd, status):
            in_send.set()
            release.wait(5)
            return inject.InjectResult(True, "sent")

        self.fake.send.side_effect = send
        self.assertEqual(self._send("old", "q-1")[0], 202)
        self.live = ["done"]
        self.assertTrue(in_send.wait(2))
        status, body = self._edit("q-1", "new")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_waiting")
        release.set()
        self.assertTrue(self._result()["ok"])
        self.assertEqual(self.fake.send.call_args[0][1]["text"], "old")

    def test_edit_just_before_job_claims_injects_new_text(self):
        # The edit wins: the job saw the session go idle but has not
        # claimed the send yet, so it must inject the edited text.
        at_final_read = threading.Event()
        edited = threading.Event()

        def status_of(sessions):
            if self.live == ["done"]:
                at_final_read.set()
                edited.wait(5)
            return list(self.live)

        self.assertEqual(self._send("old", "q-1")[0], 202)
        self.srv.state.status_of = status_of
        self.live = ["done"]
        self.assertTrue(at_final_read.wait(2))
        self.assertEqual(self._edit("q-1", "new")[0], 200)
        edited.set()
        self.assertTrue(self._result()["ok"])
        self._no_more_results()
        self.fake.send.assert_called_once()
        self.assertEqual(self.fake.send.call_args[0][1]["text"], "new")

    def test_bad_action_bodies_422(self):
        for body in (
            {"client_id": "q-1", "op": "explode"},
            {"client_id": "bad id!", "op": "cancel"},
            {"client_id": "x" * 65, "op": "cancel"},
            {"op": "cancel"},
            # Unhashable values must 422, not drop the connection.
            {"client_id": "q-1", "op": []},
            {"client_id": "q-1", "op": {}},
            {"client_id": ["q-1"], "op": "cancel"},
            {"client_id": {}, "op": "cancel"},
        ):
            status, data = self.srv.request(
                "POST", f"/v1/sessions/{self.key}/queue",
                body=body, headers=self.hdr,
            )
            self.assertEqual(status, 422, body)
            self.assertEqual(
                json.loads(data)["error"]["code"], "bad_command"
            )

    def test_removed_and_steered_sends_free_their_backlog_slots(self):
        from swe.remote import hub as hub_mod
        with patch.object(hub_mod, "MAX_SEND_BACKLOG", 4):
            for i in range(4):
                self.assertEqual(self._send(f"m{i}", f"q-{i}")[0], 202)
            status, data = self._send("full", "q-full")
            self.assertEqual(status, 429, data)
            # q-0's job is parked polling; q-1..q-3 sit behind it.
            self.assertEqual(self._act("q-1", "cancel")[0], 200)
            self.assertEqual(self._act("q-2", "cancel")[0], 200)
            self.assertEqual(self._act("q-3", "steer")[0], 200)
            for _ in range(3):
                self._result()
            for i in range(4, 7):
                status, data = self._send(f"m{i}", f"q-{i}")
                self.assertEqual(status, 202, data)
            self.assertEqual(self._send("full", "q-full")[0], 429)
        self.assertEqual(
            [s["client_id"] for s in self._list()],
            ["q-0", "q-4", "q-5", "q-6"],
        )

    def test_duplicate_client_id_while_waiting_is_409(self):
        self.assertEqual(self._send("one", "q-1")[0], 202)
        for mode in ("queue", "steer"):
            status, data = self._send("again", "q-1", mode=mode)
            self.assertEqual(status, 409, data)
            self.assertEqual(
                json.loads(data)["error"]["code"], "duplicate_client_id"
            )
        self.assertEqual(len(self._list()), 1)

    def test_cancel_as_job_starts_injecting_reports_once(self):
        # The job wins the race: it has claimed the send and is inside
        # the injector, so the cancel is refused and one result lands.
        from swe.remote import inject
        in_send = threading.Event()
        release = threading.Event()

        def send(session, cmd, status):
            in_send.set()
            release.wait(5)
            return inject.InjectResult(True, "sent")

        self.fake.send.side_effect = send
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.live = ["done"]
        self.assertTrue(in_send.wait(2))
        status, body = self._act("q-1", "cancel")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "not_waiting")
        release.set()
        result = self._result()
        self.assertTrue(result["ok"])
        self.assertNotIn("cancelled", result)
        self._no_more_results()
        self.fake.send.assert_called_once()

    def test_cancel_just_before_job_claims_never_sends(self):
        # The cancel wins: the job has seen the session go idle but not
        # yet claimed the send. It must drop it, not send it too.
        at_final_read = threading.Event()
        cancelled = threading.Event()

        def status_of(sessions):
            if self.live == ["done"]:
                at_final_read.set()
                cancelled.wait(5)
            return list(self.live)

        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.srv.state.status_of = status_of
        self.live = ["done"]
        self.assertTrue(at_final_read.wait(2))
        status, _body = self._act("q-1", "cancel")
        self.assertEqual(status, 200)
        cancelled.set()
        result = self._result()
        self.assertTrue(result["cancelled"])
        self._no_more_results()
        self.fake.send.assert_not_called()

    def test_stop_fails_a_waiting_send_once(self):
        self.assertEqual(self._send("hi", "q-1")[0], 202)
        self.assertEqual(len(self._list()), 1)
        self.srv.state.stop()
        result = self._result()
        self.assertEqual(result["note"], "hub restarted; message not sent")
        self.assertNotIn("cancelled", result)
        self._no_more_results()
        self.fake.send.assert_not_called()


class TestWaitingRegistry(unittest.TestCase):
    """sendq.Waiting: a send leaves ``waiting`` exactly once."""

    def test_edit_that_succeeds_is_always_what_gets_sent(self):
        from swe.remote import sendq
        for _ in range(50):
            reg = sendq.Waiting()
            send = sendq.QueuedSend("c1", "old", [], {"cmd": {"text": "old"}})
            reg.add("k", send)
            barrier = threading.Barrier(2)
            out = {}

            def job():
                barrier.wait()
                if reg.claim("k", send, "sending"):
                    out["sent"] = send.payload["cmd"]["text"]

            def phone():
                barrier.wait()

                def change(s):
                    s.text = "new"
                    s.payload["cmd"]["text"] = "new"

                got, why = reg.update("k", "c1", change)
                out["edited"] = got is not None
                out["why"] = why

            threads = [threading.Thread(target=job),
                       threading.Thread(target=phone)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            if out["edited"]:
                self.assertEqual(out["sent"], "new")
            else:
                self.assertEqual(out["why"], "sending")
                self.assertEqual(out["sent"], "old")

    def test_claim_is_exclusive_under_contention(self):
        from swe.remote import sendq
        for _ in range(50):
            reg = sendq.Waiting()
            send = sendq.QueuedSend("c1", "hi", [], {})
            self.assertTrue(reg.add("k", send))
            barrier = threading.Barrier(3)
            wins = []

            def by_job():
                barrier.wait()
                if reg.claim("k", send, "sending"):
                    wins.append("job")

            def by_phone(outcome):
                barrier.wait()
                got, _why = reg.take("k", "c1", outcome)
                if got is not None:
                    wins.append(outcome)

            threads = [
                threading.Thread(target=by_job),
                threading.Thread(target=by_phone, args=("removed",)),
                threading.Thread(target=by_phone, args=("steered",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            self.assertEqual(len(wins), 1, wins)
            self.assertEqual(send.state, {"job": "sending"}.get(
                wins[0], wins[0]))
            self.assertEqual(reg.list("k"), [])
            self.assertTrue(send.wake.is_set())
            self.assertEqual(reg.take("k", "c1", "removed")[1], send.state)

    def test_gone_memory_is_bounded_and_reuse_clears_it(self):
        from swe.remote import sendq
        reg = sendq.Waiting()
        with patch.object(sendq, "GONE_MEMORY", 3):
            for i in range(5):
                s = sendq.QueuedSend(f"c{i}", "x", [], {})
                reg.add("k", s)
                reg.claim("k", s, "sending")
            self.assertEqual(reg.take("k", "c0", "removed")[1], "unknown")
            self.assertEqual(reg.take("k", "c4", "removed")[1], "sending")
        again = sendq.QueuedSend("c4", "y", [], {})
        self.assertTrue(reg.add("k", again))
        self.assertIs(reg.take("k", "c4", "removed")[0], again)


_PHONE = {
    "node_name": "test-iphone.tail0000.ts.net",
    "node_short": "test-iphone",
    "node_id": "nPHONE",
    "login": "dev@github",
    "display_name": "Dev",
    "tagged": False,
}
_MAC = {
    "node_name": "macbook.tail0000.ts.net",
    "node_short": "macbook",
    "node_id": "nMAC",
    "login": "dev@github",
    "display_name": "Dev",
    "tagged": False,
}


class TestPairRoutes(unittest.TestCase):
    def setUp(self):
        self.srv = _Server(sessions=[])
        self.addCleanup(self.srv.stop)
        # Peer identity per request IP: phone asks, mac approves.
        self.srv.state.whois = (
            lambda ip: dict(_PHONE) if ip == "127.0.0.1" else None
        )
        self.srv.state.owner_login = "dev@github"

    def _req(self, name=""):
        return self.srv.request(
            "POST", "/v1/pair/request",
            body={"name": name} if name else {},
        )

    def _poll(self, rid, secret):
        return self.srv.request(
            "GET", f"/v1/pair/request/{rid}",
            headers={"X-Pair-Secret": secret},
        )

    def test_poll_secret_header_and_legacy_query(self):
        status, data = self._req("phone")
        req = json.loads(data)
        rid, secret = req["request_id"], req["poll_secret"]
        status, data = self._poll(rid, secret)
        self.assertEqual((status, json.loads(data)["status"]),
                         (200, "pending"))
        self.assertEqual(self._poll(rid, "wrong")[0], 404)
        # Shipped iPhone builds still send it in the query.
        status, _ = self.srv.request(
            "GET", f"/v1/pair/request/{rid}?secret={secret}")
        self.assertEqual(status, 200)
        self.assertEqual(self.srv.request(
            "GET", f"/v1/pair/request/{rid}")[0], 404)

    _CSRF = {"Content-Type": "application/json", "X-Swe-Pair": "1"}

    def _approve(self, code, rid=None, node=None, auth=None, headers=None):
        body = {"code": code}
        if rid:
            body["request_id"] = rid
        hdrs = dict(self._CSRF)
        hdrs.update(headers or {})
        if auth:
            hdrs.pop("X-Swe-Pair", None)
            hdrs.update(auth)
        if node is not None:
            self.srv.state.whois = lambda ip: dict(node)
            # The approval routes read whois through the cache.
            self.srv.state._whois_cache.clear()
        try:
            return self.srv.request(
                "POST", "/v1/pair/requests/approve",
                body=body, headers=hdrs,
            )
        finally:
            if node is not None:
                self.srv.state.whois = (
                    lambda ip: dict(_PHONE) if ip == "127.0.0.1" else None
                )
                self.srv.state._whois_cache.clear()

    def test_request_403_without_tailnet_identity(self):
        self.srv.state.whois = lambda ip: None
        status, data = self._req()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(data)["error"]["code"], "not_tailnet")

    def test_request_201_shape_and_poll(self):
        status, data = self._req(name="e2e")
        self.assertEqual(status, 201, data)
        r = json.loads(data)
        self.assertTrue(r["request_id"])
        self.assertTrue(r["poll_secret"])
        self.assertTrue(r["code"].isdigit() and len(r["code"]) == 6)
        status, pdata = self._poll(r["request_id"], r["poll_secret"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(pdata)["status"], "pending")
        status, _ = self._poll(r["request_id"], "wrong")
        self.assertEqual(status, 404)

    def test_owner_identity_approve_and_delivered_credential(self):
        _, data = self._req(name="phone")
        r = json.loads(data)
        # Approve from a DIFFERENT node sharing the owner's login.
        status, d = self._approve(r["code"], r["request_id"], node=_MAC)
        self.assertEqual(status, 200, d)
        self.assertTrue(json.loads(d)["ok"])
        status, pdata = self._poll(r["request_id"], r["poll_secret"])
        out = json.loads(pdata)
        self.assertEqual(out["status"], "approved")
        self.assertTrue(out["credential"])
        # The minted credential authenticates real routes.
        auth = {"Authorization": f"Bearer {out['credential']}"}
        status, _ = self.srv.request("GET", "/v1/sessions", headers=auth)
        self.assertEqual(status, 200)

    def test_same_node_cannot_self_approve(self):
        _, data = self._req()
        r = json.loads(data)
        status, d = self._approve(r["code"], r["request_id"], node=_PHONE)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "self_approval")

    def test_bearer_can_approve(self):
        cred = self.srv.pair()
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        _, data = self._req()
        r = json.loads(data)
        # whois for this peer maps to the PHONE (the requester) — the
        # bearer path must work without a valid owner identity.
        self.srv.state.owner_login = "other@github"
        status, d = self._approve(
            r["code"], r["request_id"], node=_MAC, auth=auth)
        self.assertEqual(status, 200, d)

    def test_satellite_bearer_cannot_list_or_approve(self):
        """A satellite credential is a machine's, not an approver's: it
        neither lists pending requests nor mints a phone by approving."""
        cred = self.srv.pair(role="satellite")
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        _, data = self._req()
        r = json.loads(data)
        self.srv.state.owner_login = "other@github"  # no owner identity
        status, _ = self.srv.request("GET", "/v1/pair/requests",
                                     headers=auth)
        self.assertEqual(status, 401)
        status, _ = self._approve(
            r["code"], r["request_id"], node=_MAC, auth=auth)
        self.assertEqual(status, 401)
        status, pdata = self._poll(r["request_id"], r["poll_secret"])
        self.assertEqual(json.loads(pdata)["status"], "pending")

    def test_approve_unauthenticated_401(self):
        _, data = self._req()
        r = json.loads(data)
        self.srv.state.owner_login = ""  # no owner identity possible
        status, _ = self._approve(r["code"], r["request_id"])
        self.assertEqual(status, 401)

    def test_tagged_peer_cannot_approve(self):
        _, data = self._req()
        r = json.loads(data)
        tagged = {**_MAC, "tagged": True, "login": "dev@github"}
        status, _ = self._approve(r["code"], r["request_id"], node=tagged)
        self.assertEqual(status, 401)

    def test_approve_wrong_code_404(self):
        _, data = self._req()
        r = json.loads(data)
        wrong = "000000" if r["code"] != "000000" else "999999"
        status, d = self._approve(wrong, r["request_id"], node=_MAC)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(d)["error"]["code"], "no_match")

    def test_deny_route(self):
        _, data = self._req()
        r = json.loads(data)
        # No bearer and no resolvable owner identity → 401.
        self.srv.state.owner_login = ""
        status, _ = self.srv.request(
            "POST", "/v1/pair/requests/deny",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers=dict(self._CSRF),
        )
        self.assertEqual(status, 401)
        status, d = self.srv.request(
            "POST", "/v1/pair/requests/deny",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers={
                "Content-Type": "application/json",
                "Authorization":
                    f"Bearer {self.srv.pair()['credential']}",
            },
        )
        self.assertEqual(status, 200, d)
        status, pdata = self._poll(r["request_id"], r["poll_secret"])
        self.assertEqual(json.loads(pdata)["status"], "denied")

    def test_requests_list_requires_auth_or_owner(self):
        self._req()
        self.srv.state.owner_login = ""
        status, _ = self.srv.request("GET", "/v1/pair/requests")
        self.assertEqual(status, 401)
        self.srv.state.owner_login = "dev@github"
        # Owner identity alone (no bearer) lists them too — with the
        # CSRF header a cross-site page can't send.
        status, data = self.srv.request(
            "GET", "/v1/pair/requests", headers={"X-Swe-Pair": "1"})
        self.assertEqual(status, 200)
        status, data = self.srv.request(
            "GET", "/v1/pair/requests",
            headers={
                "Authorization":
                    f"Bearer {self.srv.pair()['credential']}"
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)["requests"]), 1)

    def test_requests_list_reads_the_pre_rename_pair_header(self):
        # A tab still holding a page from before the rename sends
        # X-Quiver-Pair; it keeps working until the tab reloads.
        self._req()
        self.srv.state.owner_login = "dev@github"
        for name in ("X-Swe-Pair", "X-Quiver-Pair"):
            status, data = self.srv.request(
                "GET", "/v1/pair/requests", headers={name: "1"})
            self.assertEqual(status, 200, name)
        status, _ = self.srv.request(
            "GET", "/v1/pair/requests", headers={"X-Swe-Pair": "0"})
        self.assertEqual(status, 401)

    def test_decide_requires_json_content_type(self):
        _, data = self._req()
        r = json.loads(data)
        status, d = self.srv.request(
            "POST", "/v1/pair/requests/approve",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers={"X-Swe-Pair": "1",
                     "Content-Type": "text/plain"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "bad_origin")

    def test_decide_requires_pair_header_on_identity(self):
        _, data = self._req()
        r = json.loads(data)
        status, d = self.srv.request(
            "POST", "/v1/pair/requests/approve",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers={"Content-Type": "application/json"},
        )
        # No bearer and no X-Swe-Pair: the identity path can't
        # pass, so the hub refuses without looking the peer up.
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(d)["error"]["code"], "unauthorized")

    def test_unauthenticated_approver_never_forks_whois(self):
        calls = []
        self.srv.state.whois = lambda ip: calls.append(ip) or dict(_MAC)
        for hdrs in (
            {},  # no bearer, no pair header
            {"X-Swe-Pair": "1", "Origin": "http://evil.example"},
        ):
            status, _ = self.srv.request(
                "GET", "/v1/pair/requests", headers=hdrs)
            self.assertIn(status, (401, 403))
            status, _ = self.srv.request(
                "POST", "/v1/pair/requests/deny",
                body={"code": "123456"},
                headers={"Content-Type": "application/json", **hdrs},
            )
            self.assertIn(status, (401, 403))
        self.assertEqual(calls, [])

    def test_owner_identity_lookups_are_cached_and_rate_limited(self):
        calls = []
        self.srv.state.whois = lambda ip: calls.append(ip) or dict(_MAC)
        hdrs = {"X-Swe-Pair": "1"}
        for _ in range(5):
            status, _ = self.srv.request(
                "GET", "/v1/pair/requests", headers=hdrs)
            self.assertEqual(status, 200)
        # One lookup serves the burst.
        self.assertEqual(len(calls), 1)
        status, data = self.srv.request(
            "GET", "/v1/pair/requests", headers=hdrs)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["error"]["code"], "rate_limited")
        # A paired bearer is never throttled by the identity budget.
        status, _ = self.srv.request(
            "GET", "/v1/pair/requests",
            headers={"Authorization":
                     f"Bearer {self.srv.pair()['credential']}"},
        )
        self.assertEqual(status, 200)

    def test_decide_rejects_foreign_origin_and_host(self):
        _, data = self._req()
        r = json.loads(data)
        status, d = self._approve(
            r["code"], r["request_id"], node=_MAC,
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "bad_origin")
        # With an allowed-host set, a foreign Host is refused too.
        self.srv.state.allowed_hosts = {"100.1.2.3:8477"}
        status, d = self._approve(
            r["code"], r["request_id"], node=_MAC,
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "bad_origin")

    def test_bearer_from_same_node_is_self_approval(self):
        _, data = self._req()
        r = json.loads(data)
        # Bearer auth, but the peer IS the requesting node — the
        # whois check must still refuse self-approval.
        self.srv.state.whois = lambda ip: dict(_PHONE)
        cred = self.srv.pair()
        status, d = self.srv.request(
            "POST", "/v1/pair/requests/approve",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cred['credential']}",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "self_approval")

    def test_bearer_self_approval_rechecks_a_cached_whois_miss(self):
        _, data = self._req()
        r = json.loads(data)
        # An earlier lookup missed and the miss is still cached; the
        # node resolves now. The approval must not skip the self check.
        self.srv.state._whois_cache["127.0.0.1"] = (time.time() + 60, None)
        self.srv.state.whois = lambda ip: dict(_PHONE)
        cred = self.srv.pair()
        status, d = self.srv.request(
            "POST", "/v1/pair/requests/approve",
            body={"code": r["code"], "request_id": r["request_id"]},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cred['credential']}",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(d)["error"]["code"], "self_approval")

    def test_rate_limited_after_five(self):
        for _ in range(5):
            status, _ = self._req()
            self.assertEqual(status, 201)
        status, data = self._req()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["error"]["code"], "rate_limited")

    def test_exchange_rate_limited_after_five(self):
        # Token tries get the same per-source cap as pair requests,
        # in a budget of their own.
        for _ in range(5):
            status, _ = self.srv.request(
                "POST", "/v1/pair", {"token": "bogus"})
            self.assertEqual(status, 403)
        status, data = self.srv.request(
            "POST", "/v1/pair", {"token": "bogus"})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["error"]["code"], "rate_limited")


class TestHostCheck(unittest.TestCase):
    """The DNS-rebinding gate on the unauthenticated routes: once the
    hub knows which Host names it answers to, a request under any
    other name is refused."""

    def setUp(self):
        self.srv = _Server(sessions=[])
        self.addCleanup(self.srv.stop)
        self.srv.state.whois = lambda ip: None
        self.srv.state.allowed_hosts = {f"127.0.0.1:{self.srv.port}"}

    def test_unauthenticated_routes_reject_foreign_host(self):
        for method, path, body in (
            ("GET", "/v1/status", None),
            ("POST", "/v1/pair", {"token": "t"}),
            ("POST", "/v1/pair/request", {}),
            ("GET", "/v1/pair/request/r1?secret=s", None),
        ):
            with self.subTest(path):
                status, data = self.srv.request(
                    method, path, body,
                    headers={"Host": "rebind.example"},
                )
                self.assertEqual(status, 403, data)
                self.assertEqual(
                    json.loads(data)["error"]["code"], "bad_origin")

    def test_bind_host_passes(self):
        # http.client sends Host: 127.0.0.1:<port> on its own.
        status, _ = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        token = pairing.mint_pair_token(self.srv.home)
        status, _ = self.srv.request(
            "POST", "/v1/pair", {"token": token})
        self.assertEqual(status, 200)

    def test_empty_allowed_hosts_keeps_the_gate_open(self):
        self.srv.state.allowed_hosts = set()
        status, _ = self.srv.request(
            "GET", "/v1/status", headers={"Host": "rebind.example"})
        self.assertEqual(status, 200)


class TestDeviceMetaCapture(unittest.TestCase):
    """Pairing records kind/client/os/node_name on the device row."""

    def setUp(self):
        self.srv = _Server(sessions=[])
        self.addCleanup(self.srv.stop)
        self.srv.state.whois = lambda ip: dict(_PHONE)

    def _row(self, device_id):
        return pairing.load_devices(self.srv.home)[device_id]

    def test_exchange_explicit_fields_win(self):
        token = pairing.mint_pair_token(self.srv.home)
        status, data = self.srv.request(
            "POST", "/v1/pair",
            {"token": token, "name": "phone",
             "device": {"kind": "IPAD", "client": "SWE Remote 1.2",
                        "model": "iPad13,1"}},
            headers={"User-Agent":
                     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 "
                     "like Mac OS X) Version/18.5 Safari/604.1"},
        )
        self.assertEqual(status, 200, data)
        row = self._row(json.loads(data)["device_id"])
        # Body's kind/client beat the UA; os falls back to the UA read.
        self.assertEqual(row["kind"], "ipad")
        self.assertEqual(row["client"], "SWE Remote 1.2")
        self.assertEqual(row["model"], "iPad13,1")
        self.assertEqual(row["os"], "iOS 18.5")
        self.assertEqual(row["node_name"], _PHONE["node_name"])

    def test_exchange_ua_only(self):
        token = pairing.mint_pair_token(self.srv.home)
        status, data = self.srv.request(
            "POST", "/v1/pair", {"token": token},
            headers={"User-Agent":
                     "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                     "Chrome/125.0.6422.165 Mobile Safari/537.36"},
        )
        self.assertEqual(status, 200, data)
        row = self._row(json.loads(data)["device_id"])
        self.assertEqual(row["kind"], "android")
        self.assertEqual(row["os"], "Android 14")
        self.assertEqual(row["client"], "Chrome 125.0.6422.165")

    def test_request_flow_mints_meta(self):
        self.srv.state.owner_login = "dev@github"
        status, data = self.srv.request(
            "POST", "/v1/pair/request",
            {"device": {"kind": "iphone", "client": "SWE Remote 1.0",
                        "model": "iPhone16,2", "os": "iOS 18.5"}},
            headers={"User-Agent": "SWERemote/1.0 Darwin/25.0.0"},
        )
        self.assertEqual(status, 201, data)
        r = json.loads(data)
        # A different node approves — same-node is self-approval.
        self.srv.state.whois = lambda ip: dict(_MAC)
        status, data = self.srv.request(
            "POST", "/v1/pair/requests/approve",
            {"code": r["code"], "request_id": r["request_id"]},
            headers={"Content-Type": "application/json",
                     "Authorization":
                         f"Bearer {self.srv.pair()['credential']}"},
        )
        self.assertEqual(status, 200, data)
        row = self._row(json.loads(data)["device_id"])
        self.assertEqual(row["kind"], "iphone")
        self.assertEqual(row["model"], "iPhone16,2")
        self.assertEqual(row["client"], "SWE Remote 1.0")
        self.assertEqual(row["os"], "iOS 18.5")
        self.assertEqual(row["node_name"], _PHONE["node_name"])
        self.assertEqual(row["paired_via"], "tailscale")


class TestDeviceSelfService(unittest.TestCase):
    """GET /v1/devices/me and POST /v1/devices/rename."""

    def setUp(self):
        self.srv = _Server(sessions=[])
        self.addCleanup(self.srv.stop)
        self.cred = self.srv.pair()
        self.auth = {"Authorization": f"Bearer {self.cred['credential']}"}

    def test_me_requires_auth(self):
        status, _ = self.srv.request("GET", "/v1/devices/me")
        self.assertEqual(status, 401)

    def test_me_returns_row_without_credential(self):
        status, data = self.srv.request(
            "GET", "/v1/devices/me", headers=self.auth)
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["id"], self.cred["device_id"])
        self.assertEqual(body["name"], "test")
        self.assertNotIn("credential_hash", body)
        self.assertNotIn("credential", body)

    def test_me_owner_stub(self):
        # No bearer; the owner's own tailnet device gets a stub row
        # instead of a 404.
        self.srv.state.owner_login = "dev@github"
        self.srv.state.allowed_hosts = {"127.0.0.1"}
        self.srv.state.whois = lambda ip: dict(_MAC)
        self.srv.state._whois_cache.clear()  # the pair cached a miss
        status, data = self.srv.request(
            "GET", "/v1/devices/me",
            headers={"X-Swe-Pair": "1", "Host": "127.0.0.1"},
        )
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertTrue(body["owner"])
        self.assertEqual(body["node_name"], _MAC["node_name"])

    def test_rename_requires_auth(self):
        status, _ = self.srv.request(
            "POST", "/v1/devices/rename", {"name": "x"})
        self.assertEqual(status, 401)

    def test_rename_sets_and_clears(self):
        status, data = self.srv.request(
            "POST", "/v1/devices/rename", {"name": "Dev's phone"},
            headers=self.auth)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["name"], "Dev's phone")
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertEqual(row["display_name"], "Dev's phone")
        # Empty name clears display_name, back to the paired name.
        status, data = self.srv.request(
            "POST", "/v1/devices/rename", {"name": "  "},
            headers=self.auth)
        self.assertEqual(status, 200)
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertNotIn("display_name", row)

    def test_rename_caps_length_and_bad_body(self):
        status, data = self.srv.request(
            "POST", "/v1/devices/rename", {"name": "x" * 200},
            headers=self.auth)
        self.assertEqual(status, 200)
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertEqual(len(row["display_name"]), 80)

    def test_rename_owner_device_needs_pairing(self):
        self.srv.state.owner_login = "dev@github"
        self.srv.state.allowed_hosts = {"127.0.0.1"}
        self.srv.state.whois = lambda ip: dict(_MAC)
        self.srv.state._whois_cache.clear()  # the pair cached a miss
        status, data = self.srv.request(
            "POST", "/v1/devices/rename", {"name": "x"},
            headers={"Content-Type": "application/json",
                     "X-Swe-Pair": "1", "Host": "127.0.0.1"},
        )
        self.assertEqual(status, 409, data)
        self.assertEqual(
            json.loads(data)["error"]["code"], "needs_pairing")

    def test_backfill_on_authed_request(self):
        # A row minted before meta existed gains it on a later request
        # once the last_seen throttle lets a write through.
        pairing.update_device(
            self.srv.home, self.cred["device_id"], last_seen_at=0)
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertNotIn("kind", row)
        status, _ = self.srv.request(
            "GET", "/v1/sessions",
            headers={**self.auth, "User-Agent":
                     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 "
                     "like Mac OS X) Version/18.5 Safari/604.1"},
        )
        self.assertEqual(status, 200)
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertEqual(row["kind"], "iphone")
        self.assertEqual(row["os"], "iOS 18.5")


class TestDeviceList(unittest.TestCase):
    """GET /v1/devices and POST /v1/devices/revoke (remote/devices.py,
    the same logic the web Settings page delegates to)."""

    def setUp(self):
        self.srv = _Server(sessions=[])
        self.addCleanup(self.srv.stop)
        self.cred = self.srv.pair()
        self.auth = {"Authorization": f"Bearer {self.cred['credential']}"}

    def test_devices_requires_auth(self):
        status, _ = self.srv.request("GET", "/v1/devices")
        self.assertEqual(status, 401)

    def test_devices_lists_and_marks_this_one(self):
        other = self.srv.pair()
        status, data = self.srv.request(
            "GET", "/v1/devices", headers=self.auth)
        self.assertEqual(status, 200)
        rows = {d["id"]: d for d in json.loads(data)["devices"]}
        me = rows[self.cred["device_id"]]
        phone = rows[other["device_id"]]
        self.assertTrue(me["this_device"])
        self.assertFalse(phone["this_device"])
        # The caller's authenticated request refreshed its own row;
        # a device that never called stays None.
        self.assertIsNotNone(me["last_seen_at"])
        self.assertIsNone(phone["last_seen_at"])
        self.assertEqual(phone["paired_via"], "token")
        self.assertFalse(phone["revoked"])
        self.assertNotIn("credential_hash", phone)
        self.assertNotIn("credential", phone)

    def test_devices_includes_pending_requests(self):
        pairing.request_pair(
            self.srv.home,
            {"node_id": "n1", "node_name": "pixel", "login": "me@x"},
            "pixel")
        status, data = self.srv.request(
            "GET", "/v1/devices", headers=self.auth)
        self.assertEqual(status, 200)
        reqs = json.loads(data)["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["node_name"], "pixel")
        # Codes and secrets never leave the hub in a list.
        self.assertFalse(
            {"code", "poll_secret", "poll_secret_hash"} & set(reqs[0]))

    def test_revoke_requires_auth(self):
        status, _ = self.srv.request(
            "POST", "/v1/devices/revoke", {"device_id": "x"})
        self.assertEqual(status, 401)

    def test_revoke_another_device(self):
        other = self.srv.pair()
        status, data = self.srv.request(
            "POST", "/v1/devices/revoke",
            {"device_id": other["device_id"]}, headers=self.auth)
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertEqual(body["revoked"], other["device_id"])
        self.assertFalse(body["this_device"])
        rows = {d["id"]: d for d in body["devices"]}
        self.assertTrue(rows[other["device_id"]]["revoked"])
        # The revoked credential is dead; ours still works.
        status, _ = self.srv.request(
            "GET", "/v1/devices",
            headers={"Authorization": f"Bearer {other['credential']}"})
        self.assertEqual(status, 401)
        status, _ = self.srv.request(
            "GET", "/v1/devices", headers=self.auth)
        self.assertEqual(status, 200)

    def test_revoke_refusals(self):
        other = self.srv.pair()
        pairing.revoke_device(self.srv.home, other["device_id"])
        for body, code, want in (
            ({}, 400, "bad_device"),
            ({"device_id": 7}, 400, "bad_device"),
            ({"device_id": "x" * 65}, 400, "bad_device"),
            ({"device_id": "0123456789abcdef"}, 404, "unknown_device"),
            ({"device_id": "tailnet:n1"}, 404, "unknown_device"),
            ({"device_id": other["device_id"]}, 409, "already_revoked"),
        ):
            status, data = self.srv.request(
                "POST", "/v1/devices/revoke", body, headers=self.auth)
            self.assertEqual(
                (status, json.loads(data)["error"]["code"]),
                (code, want), body)

    def test_revoke_this_device_needs_confirm(self):
        for body in (
            {"device_id": self.cred["device_id"]},
            {"device_id": self.cred["device_id"],
             "confirm_this_device": "yes"},
        ):
            status, data = self.srv.request(
                "POST", "/v1/devices/revoke", body, headers=self.auth)
            self.assertEqual(status, 409)
            self.assertEqual(
                json.loads(data)["error"]["code"], "confirm_this_device")
        row = pairing.load_devices(self.srv.home)[self.cred["device_id"]]
        self.assertFalse(row["revoked_at"])
        status, data = self.srv.request(
            "POST", "/v1/devices/revoke",
            {"device_id": self.cred["device_id"],
             "confirm_this_device": True}, headers=self.auth)
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["this_device"])
        # We just signed ourselves out.
        status, _ = self.srv.request(
            "GET", "/v1/devices", headers=self.auth)
        self.assertEqual(status, 401)

    def test_revoke_owner_tailnet_device(self):
        # The hub owner's own tailnet sign-in can revoke a paired
        # device, and its own tailnet: sign-in too, once confirmed.
        other = self.srv.pair()
        self.srv.state.owner_login = "dev@github"
        self.srv.state.allowed_hosts = {"127.0.0.1"}
        self.srv.state.whois = lambda ip: dict(_MAC)
        self.srv.state._whois_cache.clear()  # the pairs cached a miss
        hdrs = {"Content-Type": "application/json",
                "X-Swe-Pair": "1", "Host": "127.0.0.1"}
        status, data = self.srv.request(
            "POST", "/v1/devices/revoke",
            {"device_id": other["device_id"]}, headers=hdrs)
        self.assertEqual(status, 200, data)
        self.assertFalse(json.loads(data)["this_device"])
        status, data = self.srv.request(
            "POST", "/v1/devices/revoke", {"device_id": "tailnet:nMAC"},
            headers=hdrs)
        self.assertEqual(
            (status, json.loads(data)["error"]["code"]),
            (409, "confirm_this_device"))
        status, data = self.srv.request(
            "POST", "/v1/devices/revoke",
            {"device_id": "tailnet:nMAC", "confirm_this_device": True},
            headers=hdrs)
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["this_device"])
        # Signed itself out: no owner identity for that node any more.
        status, _ = self.srv.request("GET", "/v1/devices", headers=hdrs)
        self.assertEqual(status, 401)


class TestServeEntry(unittest.TestCase):
    def test_serve_keyboard_interrupt_returns_0(self):
        from swe.remote import hub as hub_mod

        state = MagicMock()
        server = MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(hub_mod, "make_server", return_value=server):
            rc = hub_mod.serve(state, "127.0.0.1", 0)
        self.assertEqual(rc, 0)
        state.start.assert_called_once()
        state.stop.assert_called_once()
        server.server_close.assert_called_once()

    def test_serve_sigterm_takes_the_shutdown_path(self):
        # SIGTERM must reach state.stop(), not kill the process outright —
        # otherwise accepted sends drop without their command.result.
        import signal
        from swe.remote import hub as hub_mod

        state = MagicMock()
        server = MagicMock()
        shutdown_called = threading.Event()
        server.shutdown.side_effect = shutdown_called.set

        def forever(**_kw):
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)

        server.serve_forever.side_effect = forever
        prev = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(hub_mod, "make_server",
                              return_value=server):
                rc = hub_mod.serve(state, "127.0.0.1", 0)
            self.assertEqual(rc, 0)
            self.assertTrue(shutdown_called.wait(2))
            state.stop.assert_called_once()
            server.server_close.assert_called_once()
            self.assertIs(signal.getsignal(signal.SIGTERM), prev)
        finally:
            signal.signal(signal.SIGTERM, prev)

    def test_attach_seen_by_the_catalog_loop_wakes_the_tailer(self):
        # Attached outside HTTP (CLI attach, a kept tmux session taken
        # back): the catalog loop's ownership check starts it hot.
        state = HubState(temp_dir(self))
        seen = iter([
            [{"key": "claude:a", "ownership": "observed"},
             {"key": "claude:b", "ownership": "observed"}],
            [{"key": "claude:a", "ownership": "attached"},
             {"key": "claude:b", "ownership": "observed"}],
        ])
        state.catalog.rows = lambda: next(seen)
        state.catalog.refresh = lambda: []
        state._adopt_launches = lambda: None
        state._refresh_launchable = lambda: None
        # stop() wakes the loop as well; setting only _stop left it to sit
        # out the whole refresh interval.
        state.tail_manager.sync = lambda rows, **kw: state.stop()
        with patch("swe.remote.supervise.reap_kept"):
            state._catalog_loop()
        self.assertTrue(state.tail_manager.is_hot("claude:a"))
        self.assertFalse(state.tail_manager.is_hot("claude:b"))

    def test_satellite_presence_counts_survive_concurrent_streams(self):
        # Each stream connects/disconnects on its own request thread;
        # an unlocked read-modify-write lost counts and could leave a
        # machine flagged online (or offline) for good.
        import sys
        state = HubState(temp_dir(self))
        writes = []
        events = []

        def emit(_key, event):
            events.append(event["type"] == "machine.online")

        state.emit = emit

        def upsert(_home, row):
            writes.append(row["online"])

        def stream():
            for _ in range(300):
                state.sat_connected("m1")
                state.sat_disconnected("m1")

        prev = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            with patch.object(hub_mod.catalog_mod, "upsert_machine", upsert):
                # Daemon threads and a bounded join: a locking bug must
                # fail the test, not hang the run.
                threads = [threading.Thread(target=stream, daemon=True)
                           for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
        finally:
            sys.setswitchinterval(prev)
        self.assertFalse(any(t.is_alive() for t in threads),
                         "a presence worker never finished")
        self.assertEqual(state._sat_subs["m1"], 0)
        self.assertFalse(state.machine_online("m1"))
        # Flags and events match one for one, alternate online/offline,
        # and end offline.
        self.assertEqual(events, writes)
        self.assertEqual(writes[-1], False)
        self.assertTrue(all(a != b for a, b in zip(writes, writes[1:])))

    def test_failing_loop_step_logs_once_per_window(self):
        # A loop step that raises used to vanish without a trace; it
        # must reach stderr, but not on every 250 ms tick.
        import io
        import contextlib
        state = HubState(temp_dir(self))
        ticks = iter(range(3))

        def tick():
            if next(ticks) == 2:
                state._stop.set()
            raise RuntimeError("tick broke")

        state.tail_manager.tick = tick
        state.tail_manager.wait = lambda _t: None
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            state._tail_loop()
        out = err.getvalue()
        self.assertEqual(out.count("tail tick failed"), 1, out)
        self.assertIn("RuntimeError: tick broke", out)

    def test_tail_loop_sweeps_stale_journals_keeping_its_own(self):
        # Every ~5 min the tail loop sweeps idle journals and cursors,
        # sparing the global stream and each satellite's command queue.
        state = HubState(temp_dir(self))
        catalog_mod.upsert_machine(state.home, {"id": "sat1", "name": "s"})
        # A satellite session has no tailer or cursor on the hub: its
        # journal must stay while the catalog lists it.
        state.catalog.rows = lambda: [
            {"key": "claude:x@sat1", "remote": True},
            {"key": "claude:local"},
        ]
        calls = []

        def sweep(journal, keep=(), now=None):
            calls.append((journal, list(keep)))
            state._stop.set()
            return 0

        state.tail_manager.tick = lambda: None
        state.tail_manager.wait = lambda _t: None
        with patch.object(state.tail_manager, "sweep_stale", sweep), \
             patch.object(hub_mod.attachments, "sweep", lambda home: 0), \
             patch.object(hub_mod, "SWEEP_S", 0.0):
            state._tail_loop()
        self.assertEqual(len(calls), 1)
        journal, keep = calls[0]
        self.assertIs(journal, state.journal)
        self.assertIn(hub_mod.GLOBAL_KEY, keep)
        self.assertIn(state.sat_journal_key("sat1"), keep)
        self.assertIn("claude:x@sat1", keep)
        self.assertNotIn("claude:local", keep)

    def test_unwatched_hub_runs_the_tail_loop_in_low_power(self):
        # #1520: no stream, no ntfy, no recent request.
        state = HubState(temp_dir(self))
        waits = []
        state.tail_manager.tick = lambda: None

        def wait(timeout):
            waits.append((timeout, state.tail_manager.low_power))
            if len(waits) == 1:
                state.note_client()
            else:
                state._stop.set()
        state.tail_manager.wait = wait
        state._tail_loop()
        self.assertEqual(waits, [(hub_mod.TAIL_TICK_LOW_S, True),
                                 (hub_mod.TAIL_TICK_S, False)])

    def test_hot_session_keeps_the_fast_tick_in_low_power(self):
        # An attach outside HTTP opens a hot window with no client there.
        state = HubState(temp_dir(self))
        state.tail_manager.tick = lambda: None
        state.tail_manager.wake("claude:a")
        waits = []

        def wait(timeout):
            waits.append((timeout, state.tail_manager.low_power))
            state._stop.set()
        state.tail_manager.wait = wait
        state._tail_loop()
        self.assertEqual(waits, [(hub_mod.TAIL_TICK_S, True)])

    def test_inbox_catch_up_waits_for_the_github_poll(self):
        state = HubState(temp_dir(self))
        state._catalog_running = True
        state.catalog._status_as_of = time.time()  # sessions are fresh
        state._github_at = time.time() - 300       # GitHub is not
        state._catalog_wake.clear()
        state.fresh_catalog(timeout=5)              # session list: no wait
        self.assertFalse(state._catalog_wake.is_set())

        def loop():
            state._catalog_wake.wait(5)
            state._github_at = time.time()
            state._catalog_refreshed()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        state.fresh_catalog(timeout=5, github=True)
        t.join(5)
        self.assertGreater(state._github_at, time.time() - 5)

    def test_watched_counts_streams_and_recent_requests_not_satellites(self):
        state = HubState(temp_dir(self))
        self.assertFalse(state.watched())
        sat = state.bus.subscribe(state.sat_journal_key("sat1"))
        self.assertFalse(state.watched())
        # A session stream, or the global one the ntfy notifier holds.
        for key in ("claude:a", hub_mod.GLOBAL_KEY):
            q = state.bus.subscribe(key)
            self.assertTrue(state.watched(), key)
            state.bus.unsubscribe(q)
        self.assertFalse(state.watched())
        state.bus.unsubscribe(sat)
        state.note_client()
        self.assertTrue(state.watched())
        state._client_seen_at -= hub_mod.CLIENT_IDLE_S + 1
        self.assertFalse(state.watched())

    def test_client_arriving_ends_low_power_and_wakes_the_catalog(self):
        state = HubState(temp_dir(self))
        state.tail_manager.set_low_power(True)
        state.note_client()
        self.assertFalse(state.tail_manager.low_power)
        self.assertTrue(state._catalog_wake.is_set())
        # Already watched: a second request wakes nothing again.
        state._catalog_wake.clear()
        state.note_client()
        self.assertFalse(state._catalog_wake.is_set())

    def test_catalog_refreshes_once_a_minute_while_unwatched(self):
        state = HubState(temp_dir(self))
        state.catalog.refresh = lambda: []
        state._adopt_launches = lambda: None
        state._refresh_launchable = lambda: None
        state.tail_manager.sync = lambda rows, **kw: None
        state.catalog.poll_github = lambda: None
        waits = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 1:
                state.note_client()
            else:
                state._stop.set()
            return False
        state._catalog_wake.wait = wait
        with patch("swe.remote.supervise.reap_kept"):
            state._catalog_loop()
        self.assertEqual(waits, [hub_mod.CATALOG_IDLE_S,
                                 catalog_mod.REFRESH_INTERVAL_S])
        self.assertFalse(state._catalog_running)

    def test_list_request_after_low_power_waits_for_a_fresh_catalog(self):
        state = HubState(temp_dir(self))
        state._catalog_running = True
        state.catalog._status_as_of = time.time() - 300
        state._catalog_wake.clear()

        def loop():
            # The catalog loop: woken by the request, refreshes, notifies.
            state._catalog_wake.wait(5)
            state.catalog._status_as_of = time.time()
            state._catalog_refreshed()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        started = time.monotonic()
        state.fresh_catalog(timeout=5)
        t.join(5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertGreater(state.catalog.refreshed_at, time.time() - 5)

    def test_fresh_catalog_never_waits_without_a_loop_or_when_fresh(self):
        state = HubState(temp_dir(self))
        state.catalog._status_as_of = 0.0
        state.fresh_catalog(timeout=30)  # no loop running: returns
        state._catalog_running = True
        state.catalog._status_as_of = time.time()
        state.fresh_catalog(timeout=30)  # fresh already: returns
        self.assertFalse(state._catalog_wake.is_set())

    def test_loop_error_log_throttles_per_step_and_window(self):
        import io
        import contextlib
        state = HubState(temp_dir(self))

        def fail(step):
            try:
                raise RuntimeError(step)
            except RuntimeError:
                state._log_loop_error(step)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fail("tail tick")
            fail("tail tick")          # same window: quiet
            fail("catalog refresh")    # another step logs in that window
            # The window passes: the same step logs again.
            state._loop_error_at["tail tick"] -= hub_mod._LOOP_ERROR_LOG_S + 1
            fail("tail tick")
        out = err.getvalue()
        self.assertEqual(out.count("tail tick failed"), 2, out)
        self.assertEqual(out.count("catalog refresh failed"), 1, out)

    def test_catalog_loop_prunes_pair_requests_at_most_once_a_minute(self):
        home = temp_dir(self)
        state = HubState(home)
        r = pairing.request_pair(home, {"node_id": "n1"}, "phone")
        pairing.approve_request(home, r["code"])
        rows = pairing._load_requests(home)
        rows[0]["expires_at"] = time.time() - 1
        pairing._save_requests(home, rows)
        # Three loop passes: prune on the first, skip the second (same
        # minute), prune the third once the last prune is backdated
        # past the interval.
        passes = iter(range(3))
        state.catalog.rows = lambda: []
        state.catalog.refresh = lambda: []
        state.catalog.poll_github = lambda: None
        state._adopt_launches = lambda: None
        state._refresh_launchable = lambda: None
        state._catalog_wake.wait = lambda _t: None

        def sync(rows, **kw):
            if next(passes) == 2:
                state._pair_pruned_at -= hub_mod._PAIR_PRUNE_S + 1
                state._stop.set()

        state.tail_manager.sync = sync
        with patch("swe.remote.supervise.reap_kept"), \
             patch("swe.remote.pairing.prune_requests",
                   wraps=pairing.prune_requests) as prune:
            state._catalog_loop()
        self.assertEqual(prune.call_count, 2)
        # The uncollected credential left with its expired row.
        self.assertEqual(pairing._load_requests(home), [])

    def test_tail_manager_probes_through_state_status_of(self):
        state = HubState(temp_dir(self))
        state.status_of = lambda sessions: ["active"] * len(sessions)
        self.assertEqual(state.tail_manager._status_of([object()]),
                         ["active"])
        self.assertEqual(state.tail_manager._on_status,
                         state._on_probed_status)

    def test_state_start_stop(self):
        state = HubState(temp_dir(self))
        # No real session scan or notify config: only the thread
        # lifecycle is under test.
        state.catalog._fetch = lambda: []
        with patch.object(hub_mod.notify, "load_config", return_value={}):
            state.start()
        try:
            self.assertTrue(state._threads)
            self.assertTrue(all(t.is_alive() for t in state._threads))
        finally:
            state.stop()
        # stop() joins each thread with a 2s cap; a thread caught
        # mid-refresh under suite load may outlive it briefly.
        deadline = time.time() + 5
        while any(t.is_alive() for t in state._threads):
            if time.time() > deadline:
                self.fail("state.stop() left live threads")
            time.sleep(0.05)


class TestSharedFrames(unittest.TestCase):
    """One published event is cleaned and encoded once (#1532)."""

    def _event(self, text="hello"):
        ev = protocol.event("message.appended", "claude:s1", "mbp",
                            protocol.message("assistant", text))
        ev["seq"] = 7
        return ev

    def test_subscribers_share_one_encoded_frame(self):
        bus = hub_mod.EventBus()
        queues = [bus.subscribe("claude:s1") for _ in range(3)]
        real = hub_mod.injected_mod.clean_event
        with patch.object(hub_mod.injected_mod, "clean_event",
                          side_effect=real) as clean:
            bus.publish("claude:s1", self._event())
            frames = [hub_mod.sse_frame(q.get_nowait()) for q in queues]
        self.assertEqual(clean.call_count, 1)
        self.assertEqual(len(set(frames)), 1)
        self.assertTrue(frames[0].startswith(
            b"id: 7\nevent: message.appended\ndata: {"))
        self.assertTrue(frames[0].endswith(b"}\n\n"))

    def test_frame_matches_an_unshared_event(self):
        ev = self._event("café — done")
        shared = hub_mod.SharedEvent(ev)
        self.assertEqual(shared, ev)
        self.assertEqual(hub_mod.sse_frame(shared), hub_mod.sse_frame(ev))
        self.assertIn("café —".encode(), hub_mod.sse_frame(ev))

    def test_a_dropped_event_is_dropped_for_everyone(self):
        bus = hub_mod.EventBus()
        queues = [bus.subscribe("claude:s1") for _ in range(2)]
        with patch.object(hub_mod.injected_mod, "clean_event",
                          return_value=None) as clean:
            bus.publish("claude:s1", self._event())
            frames = [hub_mod.sse_frame(q.get_nowait()) for q in queues]
        self.assertEqual(frames, [None, None])
        self.assertEqual(clean.call_count, 1)

    def test_a_transient_event_has_no_id_line(self):
        ev = protocol.delta_event("claude:s1", "mbp", "m1", "assistant",
                                  "hi ", 1)
        self.assertTrue(hub_mod.sse_frame(ev).startswith(
            b"event: message.delta\n"))


class _ShortHeartbeat:
    """A closed SSE stream's handler thread only exits when a heartbeat
    write fails, and the default 15 s heartbeat left one running per test
    long after the test. A short one, kept until those threads end."""

    def setUp(self):
        super().setUp()
        streams = []
        real = hub_mod._Handler._sse_stream

        def sse_stream(handler, *args, **kwargs):
            streams.append(threading.current_thread())
            return real(handler, *args, **kwargs)

        for p in (patch.object(hub_mod, "_SSE_HEARTBEAT_S", 0.1),
                  patch.object(hub_mod._Handler, "_sse_stream", sse_stream)):
            p.start()
            self.addCleanup(p.stop)
        def join_streams():
            # Runs before the patches stop: the clients have hung up by
            # now, so each stream ends within a couple of heartbeats.
            for t in streams:
                t.join(timeout=3)
                self.assertFalse(t.is_alive(), "SSE stream still running")

        self.addCleanup(join_streams)


class TestGlobalEvents(_ShortHeartbeat, HubBase):
    def test_global_events_use_global_seq(self):
        # Live global subscribers must see the *global* journal seq on
        # mirrored events, so their ?last= cursor stays aligned with
        # replay. A direct _global emit first makes the counters differ.
        self.srv.state.emit(
            "_global",
            protocol.event("machine.online", "_global", "mbp", {}),
        )
        self.srv.state.emit(
            "claude:s1",
            protocol.event("message.appended", "claude:s1", "mbp",
                           protocol.message("assistant", "m")),
        )
        events, _ = self.srv.state.journal.read_since("_global", 0)
        # [session.status from fixture sync, machine.online, mirror]
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))  # strictly global-numbered
        mirrored = events[-1]
        self.assertEqual(mirrored["type"], "message.appended")
        self.assertEqual(
            mirrored["seq"], self.srv.state.journal.last_seq("_global")
        )
        # The session copy keeps its own numbering.
        sess_events, _ = self.srv.state.journal.read_since("claude:s1", 0)
        self.assertEqual(sess_events[0]["seq"], 1)
        self.assertNotEqual(mirrored["seq"], sess_events[0]["seq"])

    def test_global_stream_replays(self):
        self.srv.state.emit(
            "claude:s1",
            protocol.event(
                "session.status", "claude:s1", "mbp",
                {"status": "done"},
            ),
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request("GET", "/v1/events?last=0", headers=self.auth())
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream",
                          resp.getheader("Content-Type"))
            buf = b""
            for _ in range(20):
                chunk = resp.readline()
                if not chunk:
                    break
                buf += chunk
                if b"journal.caught-up" in buf:
                    break
            # The fixture's own sync already put an "active" status on
            # the global stream: look for this emit's "done".
            self.assertIn(b'"status": "done"', buf)
        finally:
            conn.close()


class TestSSE(_ShortHeartbeat, HubBase):
    def test_replay_and_live(self):
        auth = self.auth()
        key = "claude:s1"
        self.srv.state.emit(
            key, protocol.event(
                "message.appended", key, "mbp",
                protocol.message("assistant", "replay me"),
            )
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            conn.request("GET", f"/v1/sessions/{key}/events?last=0",
                         headers=auth)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            lines = {}
            got_live = False
            emitted = False
            for _ in range(200):
                line = resp.readline()
                if not line:
                    break
                line = line.decode()
                if line.startswith("event:"):
                    cur = line.split(":", 1)[1].strip()
                    lines[cur] = lines.get(cur, 0) + 1
                if "journal.caught-up" in line and not emitted:
                    # After replay marker, emit a live event.
                    emitted = True
                    self.srv.state.emit(
                        key, protocol.event(
                            "message.appended", key, "mbp",
                            protocol.message("assistant", "live"),
                        )
                    )
                if '"live"' in line:
                    got_live = True
                    break
            self.assertIn("message.appended", lines)
            self.assertTrue(got_live)
        finally:
            conn.close()

    def test_cursor_ahead_of_a_wiped_journal_resyncs(self):
        # The client last saw seq 500; the journal was wiped and holds
        # one event. It must hear journal.truncated, and a new event
        # (seq 2, under its old cursor) must still reach it.
        auth = self.auth()
        key = "claude:s1"
        self.srv.state.emit(key, protocol.event(
            "message.appended", key, "mbp",
            protocol.message("assistant", "after wipe")))
        head = self.srv.state.journal.last_seq(key)
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("GET", f"/v1/sessions/{key}/events?last=500",
                         headers=auth)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            seen = []
            got_live = False
            for _ in range(100):
                line = resp.readline().decode()
                if not line:
                    break
                seen.append(line)
                if "journal.caught-up" in line:
                    self.srv.state.emit(key, protocol.event(
                        "message.appended", key, "mbp",
                        protocol.message("assistant", "live")))
                if '"live"' in line:
                    got_live = True
                    break
            text = "".join(seen)
            self.assertIn(
                f'{{"type": "journal.truncated", "seq": {head}}}', text)
            self.assertTrue(got_live, text)
        finally:
            conn.close()

    def test_event_during_replay_is_not_duplicated(self):
        # subscribe() runs before read_since(), so an event emitted
        # while the replay is being read lands in BOTH places; the
        # stream must send it once (seq-deduped).
        key = "claude:s1"
        self.srv.state.emit(
            key, protocol.event(
                "message.appended", key, "mbp",
                protocol.message("assistant", "first"),
            )
        )
        real_read = self.srv.state.journal.read_since

        def emit_mid_replay(journal_key, after_seq, limit=None):
            self.srv.state.emit(
                key, protocol.event(
                    "message.appended", key, "mbp",
                    protocol.message("assistant", "midflight"),
                )
            )
            return real_read(journal_key, after_seq, limit)

        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            with patch.object(
                self.srv.state.journal, "read_since",
                side_effect=emit_mid_replay,
            ):
                conn.request(
                    "GET", f"/v1/sessions/{key}/events?last=0",
                    headers=self.auth(),
                )
                resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            buf = b""
            for _ in range(60):
                line = resp.readline()
                if not line:
                    break
                buf += line
                if b"journal.caught-up" in buf:
                    break
            self.assertEqual(buf.count(b"midflight"), 1)
            self.assertEqual(buf.count(b"first"), 1)
        finally:
            conn.close()

    def test_last_event_id_header_resumes_the_stream(self):
        # An EventSource client reconnects with Last-Event-ID instead
        # of ?last= — the header must win over the query arg and be
        # honored at all (otherwise every reconnect is a full replay).
        auth = self.auth()
        key = "claude:s1"
        self.srv.state.emit(
            key, protocol.event(
                "message.appended", key, "mbp",
                protocol.message("assistant", "before"),
            )
        )
        seen = self.srv.state.journal.last_seq(key)
        self.srv.state.emit(
            key, protocol.event(
                "message.appended", key, "mbp",
                protocol.message("assistant", "after"),
            )
        )
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            # Even with ?last=0 asking for everything, the header rules.
            headers = {**auth, "Last-Event-ID": str(seen)}
            conn.request(
                "GET", f"/v1/sessions/{key}/events?last=0",
                headers=headers,
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            buf = b""
            for _ in range(60):
                line = resp.readline()
                if not line:
                    break
                buf += line
                if b"journal.caught-up" in buf:
                    break
            self.assertIn(b'"after"', buf)
            self.assertNotIn(b'"before"', buf)
        finally:
            conn.close()

    def test_revoked_device_drops_stream(self):
        from swe.remote import hub as hub_mod
        cred = self.srv.pair()
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5
        )
        try:
            with patch.object(hub_mod, "_SSE_HEARTBEAT_S", 0.1), \
                    patch.object(hub_mod, "_REVOCATION_CHECK_S", 0.05):
                conn.request(
                    "GET", "/v1/events?last=0", headers=auth
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                pairing.revoke_device(self.srv.home, cred["device_id"])
                # Stream must end once the revocation check runs —
                # readline() returns "" at EOF.
                deadline = time.time() + 5
                closed = False
                while time.time() < deadline:
                    if resp.readline() == b"":
                        closed = True
                        break
                self.assertTrue(closed, "stream stayed open after revoke")
        finally:
            conn.close()


class TestServerHardening(HubBase):
    def test_stalled_request_times_out_and_hub_survives(self):
        from swe.remote import hub as hub_mod
        with patch.object(hub_mod, "_REQ_TIMEOUT_S", 0.5):
            sock = socket.create_connection(
                ("127.0.0.1", self.srv.port), timeout=5
            )
            try:
                # Headers declare a body that never arrives. Without a
                # socket deadline the handler thread would wait forever.
                sock.sendall(
                    b"POST /v1/pair HTTP/1.1\r\n"
                    b"Host: x\r\nContent-Length: 4096\r\n\r\n"
                    b"{\"token\":"
                )
                # The server must end the connection — 408, a close, or
                # an error response — well inside the patched timeout.
                sock.settimeout(4)
                try:
                    sock.recv(256)
                except socket.timeout:
                    self.fail("stalled request never timed out")
                except OSError:
                    pass  # reset counts as "ended" too
            finally:
                sock.close()
        status, _ = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)

    def test_worker_pool_is_bounded(self):
        from swe.remote import hub as hub_mod
        self.assertIsInstance(self.srv.server, hub_mod._BoundedHTTPServer)
        # Exhaust every slot; the next connection is refused at the
        # socket instead of spawning an unbounded thread.
        slots = self.srv.server._slots
        for _ in range(hub_mod._MAX_WORKERS):
            self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.srv.port, timeout=3
            )
            try:
                with self.assertRaises((http.client.HTTPException, OSError)):
                    conn.request("GET", "/v1/status")
                    conn.getresponse().read()
            finally:
                conn.close()
        finally:
            for _ in range(hub_mod._MAX_WORKERS):
                slots.release()
        status, _ = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)

    def test_listen_backlog_survives_a_burst(self):
        # Behind tailscale serve a browser's parallel asset fetches
        # become separate backend connections in the same instant; the
        # stdlib backlog of 5 lets the kernel reset the overflow.
        self.assertGreaterEqual(self.srv.server.request_queue_size, 64)
        barrier = threading.Barrier(13)
        results = []

        def fetch():
            barrier.wait()
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.srv.port, timeout=10
            )
            try:
                conn.request("GET", "/v1/status")
                results.append(conn.getresponse().status)
            finally:
                conn.close()

        threads = [threading.Thread(target=fetch, daemon=True)
                   for _ in range(12)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(results, [200] * 12)

    def test_open_session_gets_on_demand_tailer(self):
        # Drop all tailers (as if the session were past the cap), then
        # open the messages route — a tailer must appear.
        self.srv.state.tail_manager.tailers.clear()
        self.srv.state.tail_manager.statuses.clear()
        self.assertNotIn("claude:s1", self.srv.state.tail_manager.tailers)
        with patch.object(tail, "make_source",
                          return_value=MagicMock()):
            status, _ = self.srv.request(
                "GET", "/v1/sessions/claude:s1/messages",
                headers=self.auth(),
            )
        self.assertEqual(status, 200)
        self.assertIn("claude:s1", self.srv.state.tail_manager.tailers)


class TestKeepAliveAfterRefusal(HubBase):
    """A response sent before the body was read must not leave that body
    on the keep-alive connection, where it would be parsed as the start
    of the next request (which then got a 400 or 501)."""

    def setUp(self):
        super().setUp()
        self.hdr = {**self.auth(), "Content-Type": "application/json"}

    def _conn(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.srv.port, timeout=5)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def _exchange(conn, method, path, body=None, headers=None):
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read()

    def _assert_reused(self, method, path, body, headers, status, code):
        conn = self._conn()
        resp, data = self._exchange(conn, method, path, body, headers)
        self.assertEqual(resp.status, status, data)
        self.assertEqual(json.loads(data)["error"]["code"], code)
        self.assertFalse(resp.will_close, "small refused body closed")
        sock = conn.sock
        # The very same socket answers the next request properly.
        resp, data = self._exchange(conn, "GET", "/v1/status")
        self.assertEqual(resp.status, 200, data)
        self.assertTrue(json.loads(data)["ok"])
        self.assertIs(conn.sock, sock)

    def test_refusals_leave_the_connection_usable(self):
        msg = json.dumps({"text": "hello there " * 40})
        cases = (
            ("auth failure", "/v1/sessions/claude:s1/message",
             {"Content-Type": "application/json"}, 401, "unauthorized"),
            ("bad origin", "/v1/app/doctor/run",
             {**self.hdr, "Origin": "http://evil.example"},
             403, "bad_origin"),
            ("unknown session", "/v1/sessions/claude:nope/message",
             self.hdr, 404, "unknown_session"),
            ("state conflict", "/v1/sessions/claude:s1/message",
             self.hdr, 409, "not_attached"),
        )
        for label, path, headers, status, code in cases:
            with self.subTest(label):
                self._assert_reused("POST", path, msg, headers,
                                    status, code)

    def test_unread_upload_is_drained(self):
        from swe.remote import attachments
        with patch.object(attachments, "MAX_BYTES", 8):
            self._assert_reused(
                "POST", "/v1/sessions/claude:s1/attachments?name=a.txt",
                b"x" * 4096,
                {**self.auth(), "Content-Type": "application/octet-stream"},
                413, "too_large",
            )

    def test_normal_posts_reuse_the_connection(self):
        conn = self._conn()
        sock = None
        for last in (1.0, 2.0, 3.0):
            resp, data = self._exchange(
                conn, "POST", "/v1/inbox/dismiss",
                json.dumps({"key": "claude:s1", "last_active_at": last}),
                self.hdr,
            )
            self.assertEqual(resp.status, 200, data)
            self.assertFalse(resp.will_close)
            sock = sock or conn.sock
            self.assertIs(conn.sock, sock)
        resp, data = self._exchange(conn, "GET", "/v1/inbox",
                                    headers=self.hdr)
        self.assertEqual(resp.status, 200, data)
        self.assertIs(conn.sock, sock)

    def _raw(self, head: bytes, body: bytes = b"") -> bytes:
        """Send one raw request and read until the hub closes."""
        sock = socket.create_connection(
            ("127.0.0.1", self.srv.port), timeout=5)
        try:
            sock.sendall(head + body)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    self.fail("hub kept the connection open")
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            sock.close()

    def _assert_closed_cleanly(self, reply: bytes, status: int) -> None:
        head, _, body = reply.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.assertTrue(lines[0].startswith(f"HTTP/1.1 {status} "), reply)
        self.assertIn("connection: close", [ln.lower() for ln in lines])
        self.assertIn(b'"not_attached"', body)
        # Exactly one response: the body never became a second request.
        self.assertEqual(reply.count(b"HTTP/1.1 "), 1, reply)
        # And the hub is fine afterwards.
        status, _ = self.srv.request("GET", "/v1/status")
        self.assertEqual(status, 200)

    def _post_head(self, extra: str) -> bytes:
        return (
            "POST /v1/sessions/claude:s1/message HTTP/1.1\r\n"
            "Host: x\r\nContent-Type: application/json\r\n"
            f"Authorization: {self.hdr['Authorization']}\r\n"
            f"{extra}\r\n"
        ).encode()

    def test_body_over_the_drain_cap_closes_the_connection(self):
        from swe.remote import hub as hub_mod
        body = json.dumps({"text": "x" * 2000}).encode()
        follow = b"GET /v1/status HTTP/1.1\r\nHost: x\r\n\r\n"
        with patch.object(hub_mod, "_DRAIN_MAX", 64):
            reply = self._raw(
                self._post_head(f"Content-Length: {len(body)}\r\n"),
                body + follow,
            )
        self._assert_closed_cleanly(reply, 409)

    def test_slow_body_closes_instead_of_holding_the_thread(self):
        from swe.remote import hub as hub_mod
        with patch.object(hub_mod, "_DRAIN_S", 0.3):
            started = time.monotonic()
            # 400 bytes declared, 10 sent: the drain can't finish.
            reply = self._raw(
                self._post_head("Content-Length: 400\r\n"),
                b'{"text": "',
            )
        self.assertLess(time.monotonic() - started, 3)
        self._assert_closed_cleanly(reply, 409)

    def test_unframed_body_closes_the_connection(self):
        for extra in ("Transfer-Encoding: chunked\r\n",
                      "Content-Length: 1_0\r\n",
                      "Content-Length: 5\r\nContent-Length: 6\r\n",
                      # Too many digits for int() under its default
                      # limit: this raised out of parse_request.
                      f"Content-Length: {'9' * 5000}\r\n"):
            with self.subTest(extra[:40]):
                reply = self._raw(self._post_head(extra),
                                  b"5\r\nhello\r\n0\r\n\r\n")
                self._assert_closed_cleanly(reply, 409)


class TestAgentSecurity(HubBase):
    """Issue #509: agent routes need a satellite credential bound to
    the machine it names — not any paired device."""

    def _sat(self, mid="sat-m1"):
        return self.srv.satellite(mid)

    def _post(self, path, body, auth):
        return self.srv.request("POST", path, body, headers=auth)[0]

    # -- role at pair time --------------------------------------------------

    def test_pairing_assigns_role_and_register_binds(self):
        cred = self.srv.pair(role="satellite")
        row = pairing.load_devices(self.srv.home)[cred["device_id"]]
        self.assertEqual(row["role"], "satellite")
        self.assertNotIn("machine_id", row)
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        status = self._post(
            "/v1/agent/register", {"machine": {"id": "sat-m1"}}, auth)
        self.assertEqual(status, 200)
        row = pairing.load_devices(self.srv.home)[cred["device_id"]]
        self.assertEqual(row["machine_id"], "sat-m1")

    def test_pair_defaults_to_phone_and_rejects_unknown_role(self):
        cred = self.srv.pair()
        row = pairing.load_devices(self.srv.home)[cred["device_id"]]
        self.assertEqual(row["role"], "phone")
        cred = self.srv.pair(role="actions")
        row = pairing.load_devices(self.srv.home)[cred["device_id"]]
        self.assertEqual(row["role"], "phone")

    def test_satellite_credential_is_for_agent_routes_only(self):
        """Any pair token can mint a satellite row, so that row must not
        carry a phone's powers: session, launch, pairing and page routes
        all refuse it, while its own agent route still answers."""
        auth = self._sat()
        for method, path, body in (
            ("GET", "/v1/sessions", None),
            ("GET", "/v1/devices", None),
            ("GET", "/v1/app/settings", None),
            ("POST", "/v1/sessions", {"machine": "m", "harness": "claude",
                                      "cwd": "/tmp"}),
            ("POST", "/v1/devices/revoke", {"device_id": "x"}),
            ("DELETE", "/v1/sessions/claude:s1/task", None),
        ):
            status, _ = self.srv.request(method, path, body, headers=auth)
            self.assertEqual(status, 401, (method, path))
        status = self._post(
            "/v1/agent/catalog", {"machine_id": "sat-m1", "rows": []}, auth)
        self.assertEqual(status, 200)

    # -- non-satellite credentials ------------------------------------------

    def test_phone_denied_on_every_agent_route(self):
        auth = self.auth()
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": "m1"}}, auth), 403)
        self.assertEqual(
            self._post("/v1/agent/catalog",
                       {"machine_id": "m1", "rows": []}, auth), 403)
        self.assertEqual(
            self._post("/v1/agent/usage",
                       {"machine_id": "m1", "harnesses": []}, auth), 403)
        self.assertEqual(
            self._post("/v1/agent/events",
                       {"machine_id": "m1", "events": []}, auth), 403)
        status, _ = self.srv.request(
            "GET", "/v1/agent/commands?machine=m1", headers=auth)
        self.assertEqual(status, 403)
        status, _ = self.srv.request(
            "GET", "/v1/agent/attachments/claude%3Ax%40m1/r.png",
            headers=auth)
        self.assertEqual(status, 403)

    def test_legacy_row_without_role_is_a_phone(self):
        # Devices paired before roles exist have no ``role`` field —
        # they must not reach the agent routes.
        cred = self.srv.pair()
        did = cred["device_id"]
        devices = pairing.load_devices(self.srv.home)
        del devices[did]["role"]
        pairing._save_devices(self.srv.home, devices)
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": "m1"}}, auth), 403)

    def test_actions_credential_denied_on_agent_routes(self):
        # The ntfy action-button row: approval POSTs, never agent calls.
        cred = self.srv.pair()
        pairing.update_device(
            self.srv.home, cred["device_id"], role="actions")
        auth = {"Authorization": f"Bearer {cred['credential']}"}
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": "m1"}}, auth), 403)
        status, _ = self.srv.request(
            "GET", "/v1/agent/commands?machine=m1", headers=auth)
        self.assertEqual(status, 403)

    # -- machine binding ----------------------------------------------------

    def test_binding_denies_other_machines(self):
        sat = self._sat("sat-m1")
        other = "sat-m2"
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": other}}, sat), 403)
        self.assertEqual(
            self._post("/v1/agent/catalog",
                       {"machine_id": other, "rows": []}, sat), 403)
        self.assertEqual(
            self._post("/v1/agent/usage",
                       {"machine_id": other, "harnesses": []}, sat), 403)
        self.assertEqual(
            self._post("/v1/agent/events",
                       {"machine_id": other, "events": []}, sat), 403)
        status, _ = self.srv.request(
            "GET", f"/v1/agent/commands?machine={other}", headers=sat)
        self.assertEqual(status, 403)

    def test_binding_denies_other_machines_attachment(self):
        sat = self._sat("sat-m1")
        status, _ = self.srv.request(
            "GET", "/v1/agent/attachments/claude%3Ax%40sat-m2/r.png",
            headers=sat)
        self.assertEqual(status, 403)
        # A local key with no @mid is not for a satellite at all.
        status, _ = self.srv.request(
            "GET", "/v1/agent/attachments/claude%3Ax/r.png",
            headers=sat)
        self.assertEqual(status, 403)

    def test_unbound_satellite_must_register_first(self):
        cred = self.srv.pair(role="satellite")
        sat = {"Authorization": f"Bearer {cred['credential']}"}
        # No register yet: no binding, so the other routes refuse.
        self.assertEqual(
            self._post("/v1/agent/catalog",
                       {"machine_id": "m1", "rows": []}, sat), 403)
        self.assertEqual(
            self._post("/v1/agent/events",
                       {"machine_id": "m1", "events": []}, sat), 403)
        status, _ = self.srv.request(
            "GET", "/v1/agent/commands?machine=m1", headers=sat)
        self.assertEqual(status, 403)

    def test_register_is_idempotent_for_the_bound_machine(self):
        sat = self._sat("sat-m1")
        status = self._post(
            "/v1/agent/register", {"machine": {"id": "sat-m1"}}, sat)
        self.assertEqual(status, 200)

    def test_register_cannot_claim_the_hub_machine(self):
        cred = self.srv.pair(role="satellite")
        sat = {"Authorization": f"Bearer {cred['credential']}"}
        status = self._post(
            "/v1/agent/register",
            {"machine": {"id": self.srv.state.machine_id}}, sat)
        self.assertEqual(status, 403)

    def test_second_satellite_cannot_take_the_machine(self):
        # A credential stays bound to ITS machine even when another
        # machine is already registered by a different satellite.
        self._sat("sat-m1")
        sat2 = self._sat("sat-m2")
        self.assertEqual(
            self._post("/v1/agent/catalog",
                       {"machine_id": "sat-m1", "rows": []}, sat2), 403)

    def test_fresh_satellite_cannot_claim_a_bound_machine(self):
        # Registering is how a credential pins its machine; a second
        # live credential must not grab one that is already spoken for.
        self._sat("sat-m1")
        cred = self.srv.pair(role="satellite")
        sat2 = {"Authorization": f"Bearer {cred['credential']}"}
        status = self._post(
            "/v1/agent/register",
            {"machine": {"id": "sat-m1"}}, sat2)
        self.assertEqual(status, 403)

    def test_revoked_binding_frees_the_machine(self):
        # Credential replacement: revoke the old satellite, then a
        # re-paired one can register the same machine.
        cred = self.srv.pair(role="satellite")
        sat1 = {"Authorization": f"Bearer {cred['credential']}"}
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": "sat-m1"}}, sat1), 200)
        pairing.revoke_device(self.srv.home, cred["device_id"])
        cred2 = self.srv.pair(role="satellite")
        sat2 = {"Authorization": f"Bearer {cred2['credential']}"}
        self.assertEqual(
            self._post("/v1/agent/register",
                       {"machine": {"id": "sat-m1"}}, sat2), 200)

    # -- event ingest -------------------------------------------------------

    def _events(self, sat, events, mid="sat-m1"):
        status, data = self.srv.request(
            "POST", "/v1/agent/events",
            {"machine_id": mid, "events": events}, headers=sat)
        return status, json.loads(data)

    def test_unknown_event_types_are_dropped(self):
        sat = self._sat()
        status, body = self._events(sat, [
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           protocol.message("assistant", "real")),
            {"type": "command", "session": "claude:r1",
             "data": {"op": "message"}},
            {"type": "session.ownership", "session": "claude:r1",
             "data": {"ownership": "observed"}},
            {"type": "made.up", "session": "claude:r1", "data": {}},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        self.assertEqual([e["type"] for e in events],
                         ["message.appended"])

    def test_unknown_message_kinds_are_dropped(self):
        sat = self._sat()
        status, body = self._events(sat, [
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           {"kind": "<img src=x onerror=alert(1)>",
                            "text": "xss"}),
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           {"text": "kindless"}),
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           protocol.message("assistant", "real")),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        events, _ = self.srv.state.journal.read_since(
            "claude:r1@sat-m1", 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["kind"], "assistant")

    def test_agent_bookkeeping_event_types_survive(self):
        sat = self._sat()
        status, body = self._events(sat, [
            protocol.event("session.launched", "claude:r1", "sat-m1",
                           {"launch_id": "l1"}),
            protocol.event("command.result", "claude:r1", "sat-m1",
                           {"op": "message", "ok": True}),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 2)

    def test_notify_kind_checked_against_its_own_set(self):
        sat = self._sat()
        status, body = self._events(sat, [
            protocol.event("notify", "claude:r1", "sat-m1",
                           {"kind": "finished", "status": "done"}),
            protocol.event("notify", "claude:r1", "sat-m1",
                           {"kind": "assistant", "status": "done"}),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)

    def test_nonstring_kind_values_drop_not_crash(self):
        # A list or dict in data.kind used to reach frozenset
        # membership and raise TypeError, killing the whole batch.
        sat = self._sat()
        status, body = self._events(sat, [
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           {"kind": ["assistant"], "text": "x"}),
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           {"kind": {"v": "assistant"}, "text": "x"}),
            protocol.event("message.appended", "claude:r1", "sat-m1",
                           protocol.message("assistant", "real")),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)


class TestStartNotify(unittest.TestCase):
    def test_start_spawns_notifier_when_enabled(self):
        from swe.remote import hub as hub_mod, notify

        home = temp_dir(self)
        state = HubState(home)
        state.catalog._fetch = lambda: []
        cfg = {"enabled": True, "url": "https://ntfy.invalid",
               "topic": "t", "ntfy_token": "x"}
        # The notifier thread really runs: nothing it does may reach a
        # network.
        with patch.object(notify, "load_config", return_value=cfg), \
             patch.object(notify.Notifier, "_post"):
            state.start()
            try:
                names = [t.name for t in state._threads]
                self.assertIn("remote-notify", names)
            finally:
                state.stop()

    def test_start_skips_notifier_when_disabled(self):
        from swe.remote import notify

        home = temp_dir(self)
        state = HubState(home)
        state.catalog._fetch = lambda: []
        with patch.object(notify, "load_config", return_value={}):
            state.start()
            try:
                names = [t.name for t in state._threads]
                self.assertNotIn("remote-notify", names)
            finally:
                state.stop()

    def test_start_skips_notifier_on_partial_config(self):
        # enabled but no url/topic: no thread, and no undrained queue
        # left on the bus either.
        from swe.remote import notify

        home = temp_dir(self)
        state = HubState(home)
        state.catalog._fetch = lambda: []
        with patch.object(
            notify, "load_config", return_value={"enabled": True}
        ):
            state.start()
            try:
                names = [t.name for t in state._threads]
                self.assertNotIn("remote-notify", names)
                self.assertEqual(state.bus._subs, [])
            finally:
                state.stop()

    def test_start_retires_legacy_credential_when_disabled(self):
        # Notifications off must not strand the legacy ntfy-actions
        # device row: copies already sent to a topic still authenticate
        # as a paired device until the row is revoked.
        from swe.remote import notify, pairing

        home = temp_dir(self)
        device_id = "deadbeef02"
        with pairing._state_lock(home):
            devices = pairing.load_devices(home)
            devices[device_id] = {
                "name": "ntfy-actions",
                "credential_hash": pairing._hash_credential("legacy"),
                "created_at": time.time(),
                "last_seen_at": None,
                "revoked_at": None,
            }
            pairing._save_devices(home, devices)
        notify.save_config(home, {
            "enabled": False,
            "action_device_id": device_id,
            "action_credential": "legacy",
        })
        state = HubState(home)
        state.catalog._fetch = lambda: []
        state.start()
        try:
            row = pairing.load_devices(home)[device_id]
            self.assertTrue(row["revoked_at"])
            self.assertIsNone(
                pairing.device_for_credential(home, "legacy"))
            saved = notify.load_config(home)
            self.assertNotIn("action_device_id", saved)
            self.assertNotIn("action_credential", saved)
        finally:
            state.stop()


if __name__ == "__main__":
    unittest.main()
