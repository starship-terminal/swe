"""G31: a message post that must reach a keeper (``require_keeper``)
and the read-only command lookup (``GET .../commands/{id}``).

docs/specs/G11-client-core.md, "Keeper-only posts and command lookups":
a client repeats a message under its id only with ``require_keeper:
true``, so the hub must never route such a post to the legacy path,
which does not de-duplicate, and the lookup must answer what the store
holds without changing anything.
"""

import json
import queue
from unittest import mock

from swe.remote import hub as hub_mod, protocol
from swe.remote.adapters import fake

from tests.test_remote_agent import SatelliteCase, _AgentHub
from tests.test_remote_hub import _session
from tests.test_remote_hub_keeper import KeeperHubCase, Stream

import unittest


def _quote(key: str) -> str:
    from urllib.parse import quote
    return quote(key, safe="")


class LegacyRoute:
    """Counts the sends the legacy message route queues for typing in
    (``sendq.submit``), without running them. The route's ownership
    check is waved through, so a post it would take does reach it."""

    def __init__(self, case: unittest.TestCase, state) -> None:
        self.calls: list[str] = []
        calls = self.calls
        case.enterContext(mock.patch.object(
            state.sendq, "submit",
            lambda qkey, job, cancel, alive=None: calls.append(qkey)))
        case.enterContext(mock.patch.object(
            hub_mod._Handler, "_refuse_unless_attached",
            lambda handler, row: False))


class TestStatus(KeeperHubCase):
    def test_status_lists_both_features(self):
        status, out = self.call("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertIn("require_keeper", out["features"])
        self.assertIn("command_lookup", out["features"])
        self.assertEqual(protocol.FEATURE_REQUIRE_KEEPER, "require_keeper")
        self.assertEqual(protocol.FEATURE_COMMAND_LOOKUP, "command_lookup")

    def test_neither_is_listed_with_the_runtime_off(self):
        # No store: nothing is kept or can be looked up, and a satellite
        # keeper's row saying ``keeper`` must not make a client post
        # keeper-only into refusals for a day.
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()], on=False)
        status, out = self.call("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertNotIn("require_keeper", out["features"])
        self.assertNotIn("command_lookup", out["features"])
        self.assertIn("command_id", out["features"])


class TestRequireKeeperLocal(KeeperHubCase):
    def test_a_kept_session_takes_it_and_a_repeat_is_not_applied_again(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        body = {"text": "again", "command_id": "m1",
                "require_keeper": True}
        status, out = self.post(key, "message", body)
        self.assertEqual((status, out["receipt"]), (202, "accepted"), out)
        self.feed.result("m1")
        status, out = self.post(key, "message", body)
        self.assertEqual((status, out["duplicate"], out["result"]["ok"]),
                         (202, True, True), out)
        self.assertEqual(len(self.calls("send_turn")), 2)
        # The field is not part of the command: a plain repeat is the
        # same command, not a conflict.
        status, out = self.post(key, "message",
                                {"text": "again", "command_id": "m1"})
        self.assertEqual((status, out["duplicate"]), (202, True))
        self.assertNotIn("require_keeper", self.store().get_command(
            "m1").payload)

    def test_a_session_no_keeper_holds_refuses_it(self):
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()])
        legacy = LegacyRoute(self, self.srv.state)
        status, out = self.post("claude:s1", "message", {
            "text": "x", "client_id": "m1", "require_keeper": True})
        self.assertEqual((status, out["error"]["code"]), (409, "not_kept"))
        self.assertEqual(legacy.calls, [])
        self.assertEqual(self.srv.state.waiting.list("claude:s1"), [])
        # Without the field the same post takes the legacy route.
        status, _ = self.post("claude:s1", "message",
                              {"text": "x", "client_id": "m2"})
        self.assertEqual((status, len(legacy.calls)), (202, 1))

    def test_a_keeper_that_let_go_before_the_send_looked_refuses_it_as_not_kept(self):
        # Owned when do_POST picked the route, gone before ``_owned``:
        # nothing is stored or typed in, and a keeper-only message is
        # answered as the legacy routes answer it.
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()])
        legacy = LegacyRoute(self, self.srv.state)
        rt = self.srv.state.runtime
        with mock.patch.object(rt, "owns", lambda key: True):
            status, out = self.post("claude:s1", "message", {
                "text": "x", "command_id": "m1", "require_keeper": True})
            self.assertEqual((status, out["error"]["code"]),
                             (409, "not_kept"))
            status, out = self.post("claude:s1", "message", {
                "text": "x", "command_id": "m2"})
            self.assertEqual((status, out["error"]["code"]),
                             (404, "unknown_session"))
        self.assertEqual(legacy.calls, [])
        self.assertEqual(self.commands(), [])

    def test_only_true_asks_for_a_keeper(self):
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()])
        legacy = LegacyRoute(self, self.srv.state)
        for value in ("yes", 1, None, False):
            status, _ = self.post("claude:s1", "message", {
                "text": "x", "client_id": f"m{len(legacy.calls)}",
                "require_keeper": value})
            self.assertEqual(status, 202, value)
        self.assertEqual(len(legacy.calls), 4)

    def test_a_session_its_keeper_let_go_refuses_a_repeat(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        body = {"text": "again", "command_id": "m1", "require_keeper": True}
        self.assertEqual(self.post(key, "message", body)[0], 202)
        self.feed.result("m1")
        status, out = self.post(key, "control", {"op": "detach"})
        self.assertEqual(status, 200, out)
        self.assertFalse(self.srv.state.runtime.owns(key))
        legacy = LegacyRoute(self, self.srv.state)
        status, out = self.post(key, "message", body)
        self.assertEqual((status, out["error"]["code"]), (409, "not_kept"))
        self.assertEqual(legacy.calls, [])
        self.assertEqual(len(self.calls("send_turn")), 2)


class TestCommandLookup(KeeperHubCase):
    def lookup(self, key: str, command_id: str, headers=None):
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{_quote(key)}/commands/{command_id}",
            headers=self.auth() if headers is None else headers)
        return status, json.loads(data) if data else {}

    def test_a_finished_send_answers_its_receipt_and_result(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        self.assertEqual(self.post(key, "message", {
            "text": "again", "command_id": "m1"})[0], 202)
        self.feed.result("m1")
        status, out = self.lookup(key, "m1")
        self.assertEqual(status, 200, out)
        self.assertEqual(
            (out["ok"], out["session"], out["command_id"], out["op"],
             out["receipt"], out["result"]),
            (True, key, "m1", "send", "accepted",
             {"op": "send", "ok": True}))
        self.assertIsInstance(out["received_at"], float)
        self.assertNotIn("reason", out)

    def test_a_refused_send_answers_rejected_with_its_reason(self):
        key = self.launch([fake.say("one")])
        self.feed.event("turn.ended")
        self.store().record_command(
            "r1", session=key, op="send", status="queued",
            payload={"text": "x", "mode": "queue", "attachments": []},
            machine=self.srv.state.machine_id, device=None)
        self.store().transition("r1", "rejected", reason="not_recorded")
        status, out = self.lookup(key, "r1")
        self.assertEqual(
            (status, out["receipt"], out["reason"], out["result"]["ok"]),
            (200, "rejected", "not_recorded", False))

    def test_an_unknown_id_is_404(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        self.assertEqual(self.post(key, "message", {
            "text": "again", "command_id": "m1"})[0], 202)
        for session, cid in ((key, "nope"), ("claude:other", "m1"),
                             (key, "bad%20id"), (key, "x" * 65)):
            status, out = self.lookup(session, cid)
            self.assertEqual((status, out["error"]["code"]),
                             (404, "unknown_command"), (session, cid))

    def test_it_needs_a_credential(self):
        key = self.launch([fake.say("one")])
        status, out = self.lookup(key, "m1", headers={})
        self.assertEqual(status, 401, out)
        status, out = self.lookup(
            key, "m1", headers={"Authorization": "Bearer sk-test"})
        self.assertEqual(status, 401, out)

    def test_it_changes_nothing(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        self.assertEqual(self.post(key, "message", {
            "text": "again", "command_id": "m1"})[0], 202)
        self.feed.result("m1")
        self.drain(key)
        store = self.store()
        rows = store._read("SELECT * FROM commands ORDER BY id", ())
        sessions = store._read("SELECT * FROM sessions", ())
        journal = self.journal(key)
        sends = len(self.calls("send_turn"))
        stream = Stream(self, self.srv.state, key)
        everywhere = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        for cid in ("m1", "m1", "unknown"):
            self.lookup(key, cid)
        self.assertEqual(store._read("SELECT * FROM commands ORDER BY id",
                                     ()), rows)
        self.assertEqual(store._read("SELECT * FROM sessions", ()),
                         sessions)
        self.assertEqual(self.journal(key), journal)
        self.assertEqual(len(self.calls("send_turn")), sends)
        for subscriber in (stream, everywhere):
            with self.assertRaises(queue.Empty):
                subscriber.q.get_nowait()

    def test_the_slash_commands_route_is_unchanged(self):
        key = self.launch([fake.say("one")])
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{_quote(key)}/commands",
            headers=self.auth())
        self.assertEqual(status, 200, data)

    def test_with_the_runtime_off_every_id_is_unknown(self):
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()], on=False)
        self.assertIsNone(self.srv.state.runtime.store)
        status, out = self.lookup("claude:s1", "m1")
        self.assertEqual((status, out["error"]["code"]),
                         (404, "unknown_command"))


class TestRequireKeeperForward(unittest.TestCase):
    """A satellite session no keeper holds: the legacy forward."""

    def setUp(self):
        self.srv = _AgentHub()
        self.addCleanup(self.srv.stop)
        self.mid = "sat-m1"
        self.srv.request("POST", "/v1/agent/catalog", {
            "machine_id": self.mid, "rows": [{
                "key": "claude:r1", "title": "r", "status": "idle",
                "ownership": "attached", "machine_id": self.mid}]},
            headers=self.srv.sat_auth)
        self.srv.state.catalog.refresh()

    def stream(self) -> list:
        return self.srv.state.journal.read_since("_sat_sat-m1", 0)[0]

    def test_it_is_refused_and_nothing_reaches_the_machine(self):
        status, data = self.srv.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/message",
            {"text": "yo", "client_id": "cid-9", "require_keeper": True},
            headers=self.srv.auth)
        self.assertEqual((status, json.loads(data)["error"]["code"]),
                         (409, "not_kept"))
        self.assertEqual(self.stream(), [])
        # Without it the message is forwarded as before.
        status, _ = self.srv.request(
            "POST", "/v1/sessions/claude%3Ar1%40sat-m1/message",
            {"text": "yo", "client_id": "cid-9"}, headers=self.srv.auth)
        self.assertEqual((status, len(self.stream())), (202, 1))


class TestRequireKeeperSatellite(SatelliteCase):
    def running(self) -> tuple[str, str]:
        key = self.start([fake.say("one")], [fake.say("two")])
        self.sat.event("turn.ended")
        self.settle(key)
        hub_key = self.listed(key)
        self.push()
        self.assertTrue(self.agent._push_events())
        return key, hub_key

    def test_a_satellite_keeper_takes_it_once(self):
        key, hub_key = self.running()
        self.online()
        body = {"text": "again", "command_id": "m1", "require_keeper": True}
        status, out = self.post(hub_key, "message", body)
        self.assertEqual((status, out["receipt"]), (202, "queued"), out)
        status, out = self.post(hub_key, "message", body)
        self.assertEqual((status, out["duplicate"]), (202, True), out)
        self.assertEqual(len(self.stream()), 1)
        # The satellite's own route gets it keeper-only too.
        posts = []
        local_post = self.agent._local_post

        def spy(k, route, b, device=None):
            posts.append((route, b.get("require_keeper")))
            return local_post(k, route, b, device)

        self.agent._local_post = spy
        self.deliver()
        self.finished()
        self.sat.result("m1")
        self.settle(key)
        self.assertEqual(posts, [("message", True)])
        self.assertEqual(self.sends(), ["hello", "again"])
        # And the hub's store answers for it.
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{_quote(hub_key)}/commands/m1",
            headers=self.auth())
        out = json.loads(data)
        self.assertEqual((status, out["session"], out["op"]),
                         (200, hub_key, "send"))
        self.assertIn(out["receipt"], ("queued", "accepted"))

    def test_a_held_send_is_looked_up_as_held(self):
        _key, hub_key = self.running()
        self.offline()
        status, out = self.post(hub_key, "message", {
            "text": "later", "command_id": "h1", "require_keeper": True})
        self.assertEqual((status, out["receipt"]), (202, "held"), out)
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{_quote(hub_key)}/commands/h1",
            headers=self.auth())
        self.assertEqual((status, json.loads(data)["receipt"]),
                         (200, "held"))

    def test_the_satellites_route_never_types_a_kept_message_in(self):
        # The keeper let the session go after the check that let the
        # command through: the satellite's route refuses it.
        legacy = LegacyRoute(self, self.agent.local)
        self.agent.catalog._fetch = lambda: [_session("s9")]
        self.agent.catalog.refresh()
        reported = []
        self.agent._report_kept = (
            lambda cid, via, status, body:
            reported.append((cid, status, body)) or True)
        self.agent._kept_refusal = lambda *args: None
        self.agent._run_kept(1, {
            "session": "claude:s9", "route": "message", "id": "k1",
            "body": {"text": "x", "mode": "queue", "attachments": [],
                     "command_id": "k1"}}, self.agent._cmd_epoch)
        self.assertEqual(legacy.calls, [])
        [(cid, status, body)] = reported
        self.assertEqual((cid, status, body["error"]["code"]),
                         ("k1", 409, "not_kept"))


if __name__ == "__main__":
    unittest.main()
