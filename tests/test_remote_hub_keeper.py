"""The hub on session keepers (remote/runtime.py and its wiring in
hub.py, catalog.py, tail.py, fork.py).

Most tests run the hub's HTTP server over a throwaway home with the
session runtime on, and keepers on threads of this process driving the
scripted ``FakeAdapter`` (the spawner is injected). A hub is "killed"
by hanging up without any of its tidy shutdown, and a second hub is
started over the same home while the keepers run on. ``RealProcess``
starts real ``python -m swe.remote.keeper`` processes driving the fake
``claude`` under tests/fakes.

Nothing sleeps or polls: a test waits on the runtime's listener feed
(every v2 event taken into a journal), on a subscriber queue, or on an
HTTP stream. Every test stops its keepers in cleanup.
"""

import http.client
import json
import os
import plistlib
import queue
import signal
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from swe.remote import catalog as catalog_mod
from swe.remote import hub as hub_mod
from swe.remote import launchd, protocol, runtime as runtime_mod, tail
from swe.remote.adapters import events as ev
from swe.remote.adapters import fake, registry
from swe.remote.adapters.base import Caps, StartSpec, TranscriptPosition
from swe.remote.adapters.fallback import SESSION_SEED
from swe.remote.journal import Journal
from swe.remote.keeper import discovery
from swe.remote.keeper.log import read_log
from swe.remote.keeper.process import Keeper
from swe.remote.store import Store

from tests.test_remote_hub import HubBase, _Server, _session
from tests.test_remote_keeper import FAKE_CLAUDE, SRC, WAIT, kill, short_home

ADAPTER = "hubfake"
OPTIONS = [{"id": "allow", "label": "Allow", "kind": "allow"},
           {"id": "always", "label": "Always", "kind": "allow",
            "standing": True},
           {"id": "deny", "label": "Deny", "kind": "deny"}]


def registry_for(**caps) -> dict:
    session = {"adapter": ADAPTER, "steer": True, "fork": True,
               "plan_mode": True, "stop_task": True, "options": ["model"]}
    session.update(caps)
    return {"claude": {"capabilities": {"session": session}}}


class Feed:
    """The v2 events a hub took into its journals, in order."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        self.q: queue.Queue = queue.Queue()
        self.seen: list[tuple[str, dict]] = []

    def __call__(self, key: str, event: dict) -> None:
        self.q.put((key, event))

    def until(self, match, what: str) -> dict:
        while True:
            try:
                key, event = self.q.get(timeout=WAIT)
            except queue.Empty:
                self.case.fail(f"timed out waiting for {what}; saw "
                               f"{[e['type'] for _k, e in self.seen]}")
            self.seen.append((key, event))
            if match(event):
                return event

    def event(self, etype: str, **data) -> dict:
        return self.until(
            lambda e: e["type"] == etype and all(
                e["data"].get(k) == v for k, v in data.items()),
            f"{etype} {data}")

    def result(self, command_id: str) -> dict:
        return self.until(
            lambda e: e["type"] == "command.result"
            and e.get("command_id") == command_id,
            f"the result of {command_id}")


class Stream:
    """One subscriber's frames (what an SSE stream is fed), in order."""

    def __init__(self, case: unittest.TestCase, state, key: str) -> None:
        self.case = case
        self.q = state.bus.subscribe(key)
        case.addCleanup(state.bus.unsubscribe, self.q)
        self.seen: list[dict] = []

    def until(self, match, what: str) -> dict:
        while True:
            try:
                frame = self.q.get(timeout=WAIT)
            except queue.Empty:
                self.case.fail(f"timed out waiting for {what}; saw "
                               f"{[f.get('type') for f in self.seen]}")
            self.seen.append(frame)
            if match(frame):
                return frame

    def frame(self, etype: str, **data) -> dict:
        return self.until(
            lambda f: f.get("type") == etype and all(
                (f.get("data") or {}).get(k) == v for k, v in data.items()),
            f"{etype} {data}")


class HubOps:
    """What both kinds of case do to a hub: start one over ``self.home``
    with the session runtime on, kill it, call it."""

    _auth = None
    hubs: list

    def hub(self, sessions=(), statuses=None, **runtime) -> _Server:
        options = {"spawn": self.spawn, "on": True,
                   "registry": self.REGISTRY,
                   "runtime_dir": self.home / "rt",
                   "listeners": [self.feed]}
        options.update(runtime)
        srv = _Server(sessions=list(sessions), statuses=statuses,
                      home=self.home, runtime=options)
        self.hubs.append(srv)
        self.addCleanup(self.end, srv)
        return srv

    def quiet_hubs(self) -> None:
        """The test is over: a hub still up must not take the keepers
        being stopped for keepers that died."""
        for srv in self.hubs:
            srv.state.runtime._stopping = True

    def end(self, srv: _Server) -> None:
        if getattr(srv, "ended", False):
            return
        srv.ended = True
        srv.stop()

    def kill_hub(self, srv: _Server) -> None:
        """End ``srv`` the way a crash does: connections drop, nothing
        is tidied or written on the way out. The keepers run on."""
        srv.ended = True
        rt = srv.state.runtime
        rt._stopping = True
        for owned in list(rt._sessions.values()):
            if owned.client is not None:
                owned.client.close()
        srv.state._stop.set()
        srv.state.detaches.close()
        srv.server.shutdown()
        srv.server.server_close()
        srv.thread.join(timeout=3)
        rt.store.close()

    def auth(self):
        if self._auth is None:
            self._auth = HubBase.auth(self)
        return self._auth

    def call(self, method: str, path: str, body=None, srv=None):
        status, data = (srv or self.srv).request(
            method, path, body, headers=self.auth())
        return status, json.loads(data) if data else {}

    def post(self, key: str, action: str, body: dict, srv=None):
        return self.call("POST", f"/v1/sessions/{key}/{action}", body, srv)

    def journal(self, key: str, srv=None) -> list[dict]:
        return (srv or self.srv).state.journal.read_since(key, 0)[0]

    def seqs(self, key: str, srv=None) -> list[int]:
        return [e["seq"] for e in self.journal(key, srv)]

    def store(self, srv=None) -> Store:
        return (srv or self.srv).state.runtime.store

    def drain(self, key: str) -> None:
        """Return once the hub has taken in everything the keeper had
        logged: the keeper answers on the connection its events come
        down, so its answer arrives after them."""
        self.srv.state.runtime.session(key).client.request("status")


class KeeperHubCase(HubOps, HubBase):
    """A hub with the session runtime on and keepers on threads."""

    REGISTRY = registry_for()

    def setUp(self) -> None:
        self.home = short_home(self)
        self.work = self.home / "work"
        self.work.mkdir()
        self.clock = fake.FakeClock()
        self.adapters: list[fake.FakeAdapter] = []
        # One dict of FakeAdapter options per agent start, in order.
        self.plans: list[dict] = []
        self.keepers: list[Keeper] = []
        self.hubs = []
        self.specs: list[dict] = []
        self.spawn_env: list[dict | None] = []
        self.keeper_options: dict = {}
        registry.register(ADAPTER, fake.FakeAdapter, replace=True)
        self.addCleanup(registry.unregister, ADAPTER)
        self.enterContext(mock.patch(
            "swe.remote.catalog.launch_command", lambda harness: "claude"))
        self.feed = Feed(self)
        self.srv = self.hub()

    # -- keepers --------------------------------------------------------

    def factory(self, resolved):
        def make() -> fake.FakeAdapter:
            opts = dict(self.plans.pop(0)) if self.plans else {}
            adapter = fake.FakeAdapter(resolved, clock=self.clock, **opts)
            self.adapters.append(adapter)
            return adapter
        return make

    def spawn(self, key, home=None, env=None, runtime=None):
        """``discovery.spawn``'s stand-in: the keeper runs on a thread
        of this process, started from the spec the hub wrote."""
        spec = discovery.read_spec(key, home)
        self.specs.append(spec)
        self.spawn_env.append(env)
        resolved = registry.adapter_for(spec["harness"], self.REGISTRY,
                                        which=None)
        start = StartSpec(
            session_key=key, cwd=spec["cwd"], resume_id=spec["resume_id"],
            fork=spec["fork"], model=spec["model"], effort=spec["effort"],
            permission_mode=spec["permission_mode"],
            plan_mode=spec["plan_mode"], env=spec["env"],
            harness=spec["harness"], session_id=spec["session_id"])
        keeper = Keeper(
            key, start, self.factory(resolved), home=home, runtime=runtime,
            first_seq=spec["first_seq"], clock=self.clock,
            log_options={"fsync": lambda fd: None, "background": False},
            **self.keeper_options)
        keeper.start()
        self.keepers.append(keeper)
        self.addCleanup(self.stop_keeper, keeper)
        self.assertTrue(keeper.sync(WAIT))
        return discovery.find(key, home)

    def finished(self, keeper: Keeper) -> bool:
        """Whether ``keeper`` has ended, once its loop has had the time
        to finish shutting down (the hub hears the socket close a
        moment before the keeper's thread is done)."""
        keeper._done.wait(WAIT)
        return keeper.done

    def stop_keeper(self, keeper: Keeper) -> None:
        self.quiet_hubs()
        if keeper._lock_fd is not None and not keeper.done:
            keeper.stop()

    # -- helpers --------------------------------------------------------

    def launch(self, *turns, prompt: str = "hello", srv=None, **body) -> str:
        """Launch a session whose first agent plays ``turns``; returns
        its key once the launch has settled."""
        srv = srv or self.srv
        if turns or not self.plans:
            self.plans.append({"turns": list(turns)})
        stream = Stream(self, srv.state, hub_mod.GLOBAL_KEY)
        status, out = self.call("POST", "/v1/sessions", {
            "harness": "claude", "cwd": str(self.work), "prompt": prompt,
            **body}, srv)
        self.assertEqual(status, 202, out)
        launched = stream.until(
            lambda f: f.get("type") == "session.launched"
            and f["data"].get("launch_id") == out["launch_id"],
            "the launch to settle")
        self.launch_id = out["launch_id"]
        return launched["session"]

    def calls(self, name: str, adapter=None) -> list:
        adapter = adapter or self.adapters[-1]
        return [c for c in adapter.calls if c.name == name]


# ---------------------------------------------------------------------


class TestLaunchAndSend(KeeperHubCase):
    def test_a_launch_goes_through_a_keeper(self):
        key = self.launch([fake.say("hi there", chunks=["hi ", "there"])])
        self.feed.event("turn.ended", status="completed")
        self.drain(key)
        # The adapter was told the id up front, and the key is built
        # from it: nothing to adopt.
        spec = self.specs[0]
        self.assertEqual(key, f"claude:{spec['session_id']}")
        self.assertEqual((spec["resume_id"], spec["first_seq"]), ("", 1))
        self.assertEqual(self.adapters[0].session_id, spec["session_id"])
        self.assertFalse((self.home / ".swe" / "data" / "remote" /
                          "pending").exists())
        # The row: attached, held by a keeper, with the keeper's state.
        status, out = self.call("GET", f"/v1/sessions?key={key}")
        row = out["sessions"][0]
        self.assertEqual(
            (row["ownership"], row["mode"], row["control"], row["state"],
             row["status"], row["id"], row["title"], row["can_fork"]),
            ("attached", "keeper", "full", "idle", "done",
             spec["session_id"], "hello", True))
        self.assertIn("keeper", row["model_modes"])
        # The launch settled, and is answered from the store too.
        status, view = self.call("GET", f"/v1/launches/{self.launch_id}")
        self.assertEqual((view["state"], view["key"]), ("started", key))
        self.assertEqual(self.srv.state.runtime.launch_view(
            self.launch_id)["state"], "started")
        # Store: the session, its keeper, the launch, the first prompt.
        store = self.store()
        session = store.get_session(key)
        self.assertEqual(
            (session.harness, session.harness_session_id, session.state,
             session.adapter, session.cwd, session.control),
            ("claude", spec["session_id"], "idle", ADAPTER, str(self.work),
             "full"))
        keepers = store.list_keepers(session=key)
        self.assertEqual([(k.state, k.pid) for k in keepers],
                         [("live", os.getpid())])
        prompt = store.get_command(f"{self.launch_id}-prompt")
        self.assertEqual((prompt.op, prompt.status, prompt.result["ok"],
                          prompt.payload["text"]),
                         ("send", "accepted", True, "hello"))
        self.assertGreater(store.get_session(key).last_seq, 0)

    def test_send_answers_with_the_keepers_receipt(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        status, out = self.post(key, "message",
                                {"text": "again", "client_id": "m1"})
        self.assertEqual(status, 202, out)
        self.assertEqual(
            (out["receipt"], out["waiting"], out["client_id"],
             out["command_id"], out["queued"]),
            ("accepted", False, "m1", "m1", True))
        self.feed.result("m1")
        self.drain(key)
        sent = self.calls("send_turn")
        self.assertEqual([c.args[0].text for c in sent], ["hello", "again"])
        self.assertEqual(sent[1].args[0].command_id, "m1")
        cmd = self.store().get_command("m1")
        self.assertEqual((cmd.status, cmd.result, cmd.session),
                         ("accepted", {"op": "send", "ok": True}, key))
        # Every event is in the journal once, under the keeper's seq,
        # as the v2 event it is.
        events = self.journal(key)
        self.assertEqual([e["seq"] for e in events],
                         list(range(1, len(events) + 1)))
        self.assertTrue(all(e["v"] == 2 for e in events))
        log = read_log(key, home=self.home).events
        self.assertEqual(events, log)

    def test_a_send_with_no_id_gets_one_and_is_not_deduplicated(self):
        key = self.launch([fake.say("one")], [fake.say("a")],
                          [fake.say("b")])
        self.feed.event("turn.ended")
        ids = []
        for _ in range(2):
            status, out = self.post(key, "message", {"text": "same"})
            self.assertEqual(status, 202, out)
            self.assertEqual(out["client_id"], "")
            ids.append(out["command_id"])
            self.feed.event("turn.ended")
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(i.startswith("h") and len(i) == 25 for i in ids))
        self.assertEqual(len(self.calls("send_turn")), 3)

    def test_a_send_mid_turn_waits_in_the_keepers_queue(self):
        key = self.launch([fake.ask("run it?", options=OPTIONS)],
                          [fake.say("second")])
        asked = self.feed.event("approval.requested")
        status, out = self.post(key, "message",
                                {"text": "next", "client_id": "q1"})
        self.assertEqual((status, out["receipt"], out["waiting"]),
                         (202, "queued", True))
        self.feed.event("queue.updated")
        status, listing = self.call("GET", f"/v1/sessions/{key}/queue")
        self.assertEqual([(s["client_id"], s["text"])
                          for s in listing["sends"]], [("q1", "next")])
        self.assertEqual(self.store().get_command("q1").status, "queued")
        # Edit it, then let the turn end: the edited text is sent.
        status, out = self.post(key, "queue", {
            "client_id": "q1", "op": "edit", "text": "next, edited"})
        self.assertEqual((status, out["text"]), (200, "next, edited"))
        status, out = self.post(key, "approval", {
            "request_id": asked["data"]["request_id"], "decision": "allow"})
        self.assertEqual(status, 200, out)
        self.feed.result("q1")
        self.assertEqual(self.calls("send_turn")[-1].args[0].text,
                         "next, edited")
        self.assertEqual(self.store().get_command("q1").status, "accepted")

    def test_a_queued_message_can_be_removed_or_sent_now(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        self.feed.event("approval.requested")
        for cid in ("q1", "q2"):
            self.assertEqual(self.post(key, "message", {
                "text": f"text {cid}", "client_id": cid})[0], 202)
        self.feed.until(lambda e: e["type"] == "queue.updated"
                        and len(e["data"]["items"]) == 2, "two queued")
        status, out = self.post(key, "queue",
                                {"client_id": "q1", "op": "cancel"})
        self.assertEqual(status, 200, out)
        removed = self.feed.result("q1")
        self.assertEqual((removed["data"]["ok"],
                          removed["data"].get("cancelled")), (False, True))
        self.assertTrue(self.store().get_command("q1").result["cancelled"])
        # Send now: it leaves the queue and goes as a steer.
        status, out = self.post(key, "queue",
                                {"client_id": "q2", "op": "steer"})
        self.assertEqual(status, 200, out)
        self.feed.until(lambda e: e["type"] == "message.completed"
                        and e["data"]["text"] == "text q2", "the steer")
        self.assertEqual([c.args[0].text for c in self.calls("steer")],
                         ["text q2"])
        status, listing = self.call("GET", f"/v1/sessions/{key}/queue")
        self.assertEqual(listing["sends"], [])
        status, out = self.post(key, "queue",
                                {"client_id": "q1", "op": "cancel"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "not_waiting"))

    def test_a_launch_retried_under_its_id_is_one_launch(self):
        self.plans.append({"turns": [[fake.say("hi")]]})
        body = {"harness": "claude", "cwd": str(self.work), "prompt": "go",
                "command_id": "launch-1"}
        stream = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        status, first = self.call("POST", "/v1/sessions", body)
        stream.frame("session.launched")
        status, again = self.call("POST", "/v1/sessions", body)
        self.assertEqual((status, again["launch_id"], again["duplicate"],
                          again["state"]),
                         (202, first["launch_id"], True, "started"))
        self.assertEqual(len(self.keepers), 1)
        self.assertEqual(len(self.store().list_launches()), 1)

    def test_a_launch_that_cannot_start_fails_and_is_let_go(self):
        def no_keeper(key, **kw):
            raise discovery.SpawnError("the keeper did not come up",
                                       "line one\nno such adapter")
        self.srv.state.runtime.spawn = no_keeper
        status, out = self.call("POST", "/v1/sessions", {
            "harness": "claude", "cwd": str(self.work), "prompt": "go"})
        self.assertEqual(status, 202, out)
        store = self.store()
        # The launch runs on a thread of its own: wait for that thread.
        for thread in threading.enumerate():
            if thread.name == f"remote-launch-{out['launch_id']}":
                thread.join(WAIT)
        self.assertEqual(store.get_launch(out["launch_id"]).state, "failed")
        status, view = self.call("GET", f"/v1/launches/{out['launch_id']}")
        self.assertEqual(view["state"], "failed")
        self.assertIn("no such adapter", view["note"])
        self.assertEqual(self.srv.state.runtime.rows(), [])
        self.assertEqual(store.list_sessions()[0].state, "closed")

    def test_a_yolo_launch_keeps_the_old_path(self):
        with mock.patch("swe.remote.supervise.fresh_launch_error",
                        return_value=None), \
                mock.patch("swe.remote.supervise.acp_launch_eligible",
                           return_value=False), \
                mock.patch("swe.remote.supervise.tmux_bin",
                           return_value=None):
            status, out = self.call("POST", "/v1/sessions", {
                "harness": "claude", "cwd": str(self.work), "prompt": "go",
                "yolo": True})
        self.assertEqual((status, out["error"]["message"]),
                         (422, "tmux is not installed"))
        self.assertEqual(self.keepers, [])

    def test_model_and_provider_reach_the_keeper(self):
        keys = self.home / "keys"
        keys.write_text("export ANTHROPIC_API_KEY=sk-test\n")
        with mock.patch("swe.remote.launch_provider.keys_path",
                        return_value=keys), \
                mock.patch("swe.remote.launch_provider.provider_error",
                           return_value=None), \
                mock.patch("swe.remote.launch_provider.provider_env",
                           return_value={"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch("swe.remote.supervise.fresh_launch_error",
                           return_value=None):
            key = self.launch([fake.say("hi")], model="opus",
                              provider="anthropic")
        self.assertEqual(self.specs[0]["model"], "opus")
        # The key rides the keeper's environment, never spec.json.
        self.assertEqual(self.spawn_env[0], {"ANTHROPIC_API_KEY": "sk-test"})
        spec_text = (discovery.keeper_dir_for(key, self.home) /
                     "spec.json").read_text()
        self.assertNotIn("sk-test", spec_text)
        self.assertEqual(self.store().get_session(key).options,
                         {"model": "opus", "provider": "anthropic"})


class TestDuplicates(KeeperHubCase):
    def test_a_retried_send_is_applied_once(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        body = {"text": "again", "client_id": "m1"}
        self.assertEqual(self.post(key, "message", body)[0], 202)
        self.feed.result("m1")
        status, out = self.post(key, "message", body)
        self.assertEqual(status, 202, out)
        self.assertEqual(
            (out["duplicate"], out["receipt"], out["result"],
             out["waiting"]),
            (True, "accepted", {"op": "send", "ok": True}, False))
        # ``command_id`` is the same id under its new name.
        status, out = self.post(key, "message",
                                {"text": "again", "command_id": "m1"})
        self.assertTrue(out["duplicate"])
        self.assertEqual(len(self.calls("send_turn")), 2)
        self.assertEqual(
            len([e for e in self.journal(key)
                 if e["type"] == "command.receipt"
                 and e.get("command_id") == "m1"]), 1)

    def test_an_id_reused_for_another_command_is_refused(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        self.assertEqual(self.post(key, "message", {
            "text": "first", "client_id": "m1"})[0], 202)
        self.feed.result("m1")
        status, out = self.post(key, "message",
                                {"text": "other", "client_id": "m1"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "command_id_conflict"))
        status, out = self.post(key, "message", {
            "text": "x", "client_id": "a", "command_id": "b"})
        self.assertEqual((status, out["error"]["code"]),
                         (422, "bad_command"))
        self.assertEqual(len(self.calls("send_turn")), 2)

    def test_the_keeper_refuses_an_id_the_store_forgot(self):
        # The store is the first guard; the keeper's own memory of ids
        # is the second.
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        self.assertEqual(self.post(key, "message", {
            "text": "first", "client_id": "m1"})[0], 202)
        self.feed.result("m1")
        with self.store()._write() as db:
            db.execute("DELETE FROM commands WHERE command_id = 'm1'")
        status, out = self.post(key, "message",
                                {"text": "first", "client_id": "m1"})
        self.assertEqual((status, out["receipt"]), (202, "accepted"))
        self.assertEqual(len(self.calls("send_turn")), 2)


class TestApprovals(KeeperHubCase):
    def test_two_clients_answer_one_approval_and_the_first_wins(self):
        key = self.launch([fake.ask("run ls?", options=OPTIONS),
                           fake.say("done")])
        asked = self.feed.event("approval.requested")
        rid = asked["data"]["request_id"]
        self.assertEqual(self.srv.state.pending_approvals, {key: rid})
        status, first = self.post(key, "approval", {
            "request_id": rid, "decision": "allow", "client_id": "phone"})
        status2, second = self.post(key, "approval", {
            "request_id": rid, "decision": "deny", "client_id": "mac"})
        self.assertEqual((status, first["receipt"]), (200, "accepted"))
        self.assertEqual((status2, second["error"]["code"]),
                         (409, "approval_failed"))
        self.assertIn("already answered", second["error"]["message"])
        self.feed.event("turn.ended", status="completed")
        answers = self.calls("respond")
        self.assertEqual([(c.args[0], c.args[1].decision) for c in answers],
                         [(rid, "allow")])
        resolved = [e for e in self.journal(key)
                    if e["type"] == "approval.resolved"]
        self.assertEqual([(e["data"]["decision"], e["data"]["source"])
                          for e in resolved], [("allow", "user")])
        self.assertEqual(self.srv.state.pending_approvals, {})
        store = self.store()
        self.assertTrue(store.get_command("phone").result["ok"])
        lost = store.get_command("mac")
        self.assertEqual((lost.status, lost.result["ok"]),
                         ("rejected", False))
        # The loser's retry gets the same refusal back, not a second try.
        status3, again = self.post(key, "approval", {
            "request_id": rid, "decision": "deny", "client_id": "mac"})
        self.assertEqual((status3, again["duplicate"], again["receipt"]),
                         (200, True, "rejected"))

    def test_a_menu_row_number_names_the_option(self):
        key = self.launch([fake.ask("which?", options=OPTIONS)])
        rid = self.feed.event("approval.requested")["data"]["request_id"]
        status, out = self.post(key, "approval",
                                {"request_id": rid, "decision": "9"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "approval_failed"))
        status, out = self.post(key, "approval",
                                {"request_id": rid, "decision": "2"})
        self.assertEqual(status, 200, out)
        answer = self.calls("respond")[0].args[1]
        self.assertEqual((answer.option_id, answer.decision),
                         ("always", "always"))

    def test_an_answer_needs_a_running_session(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        rid = self.feed.event("approval.requested")["data"]["request_id"]
        kill(self.keepers[0])
        stream = Stream(self, self.srv.state, key)
        stream.frame("session.status", status="error")
        status, out = self.post(key, "approval",
                                {"request_id": rid, "decision": "allow"})
        self.assertEqual((status, out["error"]["message"]),
                         (409, "the session is not running"))


class TestControl(KeeperHubCase):
    def test_interrupt_and_model(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        self.feed.event("approval.requested")
        status, out = self.post(key, "control", {"op": "interrupt"})
        self.assertEqual(status, 200, out)
        self.feed.event("turn.ended", status="interrupted")
        self.assertEqual(len(self.calls("interrupt")), 1)
        status, out = self.post(key, "control",
                                {"op": "model", "value": "opus"})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.adapters[0].options["model"], "opus")
        # Effort is not one of this adapter's options.
        status, out = self.post(key, "control",
                                {"op": "effort", "value": "high"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "control_failed"))
        status, out = self.post(key, "control", {"op": "compact"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "control_failed"))
        status, out = self.post(key, "control", {"op": "attach"})
        self.assertEqual((status, out["note"]), (200, "already attached"))

    def test_the_command_route_reaches_ops_v1_has_no_name_for(self):
        key = self.launch([fake.task("t1", title="look around"),
                           fake.ask("go?", options=OPTIONS)])
        self.feed.event("approval.requested")
        status, out = self.post(key, "command", {
            "op": "set_option", "name": "plan_mode", "value": True,
            "command_id": "p1"})
        self.assertEqual((status, out["receipt"], out["command_id"]),
                         (200, "accepted", "p1"))
        self.feed.result("p1")
        self.assertIs(self.adapters[0].options["plan_mode"], True)
        # Stored and deduplicated like every other write.
        status, out = self.post(key, "command", {
            "op": "set_option", "name": "plan_mode", "value": True,
            "command_id": "p1"})
        self.assertEqual((status, out["receipt"]), (200, "accepted"))
        self.assertEqual(len(self.calls("set_option")), 1)
        status, out = self.post(key, "command",
                                {"op": "stop_task", "task_id": "t1"})
        self.assertEqual(status, 200, out)
        self.feed.event("task.updated", status="stopped")
        status, out = self.post(key, "command",
                                {"op": "stop_task", "task_id": "nope"})
        self.assertEqual(status, 200, out)
        status, out = self.post(key, "command", {
            "op": "set_option", "name": "effort", "value": "high"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "command_rejected"))
        status, out = self.post(key, "command", {"op": "rm -rf"})
        self.assertEqual((status, out["error"]["code"]),
                         (422, "bad_command"))
        status, out = self.post(key, "command", {
            "op": "interrupt", "clear_queue": True})
        self.assertEqual(status, 200, out)
        self.feed.event("turn.ended", status="interrupted")

    def test_the_command_route_is_for_kept_sessions_only(self):
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session()])
        status, out = self.post("claude:s1", "command", {"op": "park"})
        self.assertEqual((status, out["error"]["code"]), (409, "not_kept"))

    def test_detach_stops_the_keeper_and_lets_the_session_go(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        stream = Stream(self, self.srv.state, key)
        status, out = self.post(key, "control",
                                {"op": "detach", "mode": "keep"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "control_failed"))
        status, out = self.post(key, "control", {"op": "detach"})
        self.assertEqual(status, 200, out)
        self.assertTrue(self.finished(self.keepers[0]))
        info = discovery.find(key, self.home)
        self.assertEqual((info.alive, info.state), (False, "exited"))
        own = stream.frame("session.ownership")
        self.assertEqual((own["data"]["ownership"], own["data"]["mode"]),
                         ("observed", None))
        rt = self.srv.state.runtime
        self.assertFalse(rt.owns(key))
        self.assertEqual(self.store().get_session(key).state, "closed")
        # The keeper's last words are in the journal, and the hub's own
        # ownership event follows them under the next seq.
        events = self.journal(key)
        self.assertEqual(events[-2]["data"], {"state": "closed"})
        self.assertEqual(events[-1]["type"], "session.ownership")
        self.assertEqual(events[-1]["seq"], events[-2]["seq"] + 1)
        status, out = self.post(key, "message", {"text": "x"})
        self.assertIn(status, (404, 409))

    def test_detach_when_idle_waits_for_the_turn(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        rid = self.feed.event("approval.requested")["data"]["request_id"]
        stream = Stream(self, self.srv.state, key)
        status, out = self.post(key, "control",
                                {"op": "detach", "mode": "when_idle"})
        self.assertEqual((status, out["mode"]), (202, "when_idle"))
        self.assertTrue(self.srv.state.runtime.owns(key))
        self.post(key, "approval", {"request_id": rid, "decision": "allow"})
        stream.frame("session.ownership", ownership="observed")
        self.assertFalse(self.srv.state.runtime.owns(key))


class TestHubRestart(KeeperHubCase):
    def test_kill_the_hub_mid_turn_then_answer_the_approval(self):
        # The agent is stepped by hand, so it can keep working while no
        # hub is there to hear it.
        self.plans.append({"autoplay": False, "turns": [
            [fake.say("looking"), fake.tool("Bash", input={"command": "ls"},
                                            output="a b"),
             fake.ask("delete a?", options=OPTIONS), fake.say("deleted")],
            [fake.say("and the next")]]})
        key = self.launch()
        adapter = self.adapters[0]
        self.assertTrue(adapter.step())
        self.feed.event("message.completed", text="looking")
        status, out = self.post(key, "message",
                                {"text": "then this", "client_id": "q1"})
        self.assertEqual(out["receipt"], "queued")
        self.feed.event("queue.updated")
        before = self.seqs(key)

        self.kill_hub(self.srv)
        # No hub: the tool runs and the approval is asked meanwhile.
        self.assertTrue(adapter.step())
        self.assertTrue(adapter.step())
        self.assertEqual(self.seqs(key), before)
        self.assertEqual(self.keepers[0].status()["pending"], ["r1"])

        self.feed = Feed(self)
        self.srv = srv = self.hub()
        rt = srv.state.runtime
        # Reconnected to the keeper it could not take the lock of, and
        # replayed from the journal's last seq.
        self.assertEqual(len(self.keepers), 1)
        self.assertTrue(rt.session(key).connected)
        replayed = [e["seq"] for _k, e in self.feed_now()]
        self.assertEqual(replayed[0], before[-1] + 1)
        # The pending approval and the queue are intact.
        self.assertEqual(srv.state.pending_approvals, {key: "r1"})
        status, listing = self.call("GET", f"/v1/sessions/{key}/queue")
        self.assertEqual([s["client_id"] for s in listing["sends"]], ["q1"])
        status, out = self.call("GET", f"/v1/sessions?key={key}")
        self.assertEqual(
            (out["sessions"][0]["status"], out["sessions"][0]["state"]),
            ("followup", "waiting"))
        status, inbox = self.call("GET", "/v1/inbox")
        self.assertIn(key, [item.get("key") for item in inbox["items"]])

        status, out = self.post(key, "approval", {
            "request_id": "r1", "decision": "allow", "client_id": "ok"})
        self.assertEqual((status, out["receipt"]), (200, "accepted"))
        adapter.play()
        self.feed.event("turn.ended", status="completed")
        # The queued message then went by itself.
        self.feed.result("q1")
        adapter.play()
        self.feed.event("turn.ended", status="completed")
        self.drain(key)

        # No gap, nothing twice: the journal is the keeper's log.
        seqs = self.seqs(key)
        self.assertEqual(seqs, list(range(1, seqs[-1] + 1)))
        self.assertEqual(self.journal(key),
                         read_log(key, home=self.home).events)
        store = self.store()
        self.assertTrue(store.get_command("q1").result["ok"])
        self.assertEqual(store.unfinished(), [])
        self.assertEqual([c.args[0].text for c in self.calls("send_turn")],
                         ["hello", "then this"])

    def feed_now(self) -> list[tuple[str, dict]]:
        """What the feed holds at this moment (the replay a new hub
        took in while it opened)."""
        out = []
        while True:
            try:
                out.append(self.feed.q.get_nowait())
            except queue.Empty:
                return out

    def test_a_command_the_dead_hub_never_delivered_is_delivered(self):
        key = self.launch([fake.say("one")], [fake.say("two")])
        self.feed.event("turn.ended")
        store = self.store()
        # What a hub leaves when it dies between the receipt and the
        # delivery: stored, and unknown to the keeper.
        store.record_command(
            "m9", session=key, op="send", status="queued",
            payload={"text": "after the crash", "mode": "queue",
                     "attachments": []},
            machine=self.srv.state.machine_id)
        store.record_command(
            "i9", session=key, op="interrupt", status="queued", payload={},
            machine=self.srv.state.machine_id)
        self.kill_hub(self.srv)
        self.feed = Feed(self)
        self.srv = self.hub()
        self.feed.result("m9")
        self.assertEqual(self.calls("send_turn")[-1].args[0].text,
                         "after the crash")
        store = self.store()
        self.assertTrue(store.get_command("m9").result["ok"])
        # A Stop is not delivered late: it would end another turn.
        late = store.get_command("i9")
        self.assertEqual((late.result["ok"], self.calls("interrupt")),
                         (False, []))
        self.assertIn("hub restarted", late.result["note"])
        # A retry of the delivered one is still a duplicate.
        status, out = self.post(key, "message", {
            "text": "after the crash", "client_id": "m9"})
        self.assertTrue(out["duplicate"])

    def test_an_answer_the_dead_hub_never_delivered_is_delivered(self):
        key = self.launch([fake.ask("go?", options=OPTIONS),
                           fake.say("went")])
        rid = self.feed.event("approval.requested")["data"]["request_id"]
        self.drain(key)
        store = self.store()
        mid = self.srv.state.machine_id
        store.record_command(
            "a9", session=key, op="respond", status="queued",
            payload={"request_id": rid, "decision": "allow"}, machine=mid,
            device="phone-1")
        # A menu row is only a number once the hub that saw the menu is
        # gone: that one is failed, not guessed at.
        store.record_command(
            "a8", session=key, op="respond", status="queued",
            payload={"request_id": rid, "decision": "2"}, machine=mid)
        self.kill_hub(self.srv)
        self.feed = Feed(self)
        self.srv = self.hub()
        self.feed.event("turn.ended", status="completed")
        answer = self.calls("respond")[0].args[1]
        self.assertEqual((answer.decision, answer.device),
                         ("allow", "phone-1"))
        store = self.store()
        self.assertTrue(store.get_command("a9").result["ok"])
        self.assertFalse(store.get_command("a8").result["ok"])
        self.assertEqual(len(self.calls("respond")), 1)

    def test_a_keeper_the_store_does_not_know_is_taken_in(self):
        key = self.launch([fake.say("hi")], [fake.say("still here")])
        self.feed.event("turn.ended")
        self.drain(key)
        self.kill_hub(self.srv)
        remote = self.home / ".swe" / "data" / "remote"
        for path in remote.glob("remote.sqlite*"):
            path.unlink()
        self.feed = Feed(self)
        self.srv = self.hub()
        rt = self.srv.state.runtime
        self.assertTrue(rt.owns(key))
        stored = self.store().get_session(key)
        self.assertEqual((stored.harness, stored.harness_session_id,
                          stored.cwd),
                         ("claude", self.specs[0]["session_id"],
                          str(self.work)))
        self.assertEqual(self.post(key, "message", {"text": "more"})[0], 202)
        self.feed.event("turn.ended")
        self.assertEqual(len(self.keepers), 1)

    def test_a_store_this_swe_cannot_open_turns_the_runtime_off(self):
        import sqlite3

        self.kill_hub(self.srv)
        path = self.home / ".swe" / "data" / "remote" / "remote.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        before = path.read_bytes()
        with mock.patch("builtins.print") as said:
            self.srv = self.hub(sessions=[_session()])
        rt = self.srv.state.runtime
        self.assertEqual((rt.enabled, rt.active, rt.can_drive("claude")),
                         (False, False, False))
        self.assertIn("session runtime off", said.call_args.args[0])
        self.assertEqual(path.read_bytes(), before)
        # The hub itself still serves.
        status, out = self.call("GET", "/v1/sessions")
        self.assertEqual((status, len(out["sessions"])), (200, 1))
        rt.stop()

    def test_a_launch_cut_off_by_the_restart_is_settled(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        store = self.store()
        store.add_launch("lgone", machine=self.srv.state.machine_id,
                         harness="claude", cwd=str(self.work),
                         session="claude:never")
        store.add_launch("lhere", machine=self.srv.state.machine_id,
                         harness="claude", cwd=str(self.work), session=key)
        self.kill_hub(self.srv)
        self.srv = self.hub()
        status, view = self.call("GET", "/v1/launches/lgone")
        self.assertEqual(view["state"], "failed")
        self.assertIn("restarted", view["note"])
        status, view = self.call("GET", "/v1/launches/lhere")
        self.assertEqual((view["state"], view["key"]), ("started", key))

    def test_a_clean_stop_leaves_the_keepers_running(self):
        key = self.launch([fake.say("hi")], [fake.say("again")])
        self.feed.event("turn.ended")
        self.end(self.srv)
        self.assertFalse(self.keepers[0].done)
        self.feed = Feed(self)
        self.srv = self.hub()
        self.assertEqual(self.post(key, "message", {"text": "more"})[0], 202)
        self.feed.event("turn.ended")
        self.assertEqual(len(self.keepers), 1)

    def test_a_keeper_from_another_version_is_cycled_when_idle(self):
        self.plans.append({"turns": [[fake.ask("go?", options=OPTIONS)]]})
        self.plans.append({"turns": [[fake.say("on the new code")]]})
        key = self.launch()
        rid = self.feed.event("approval.requested")["data"]["request_id"]
        rt = self.srv.state.runtime
        folder = discovery.keeper_dir_for(key, self.home)
        info = discovery.read_json(folder / "keeper.json")
        discovery.write_json(folder / "keeper.json",
                             {**info, "swe_version": "0.0.1"})
        # Mid-turn: left alone.
        rt.tick()
        self.assertFalse(self.keepers[0].done)
        self.post(key, "approval", {"request_id": rid, "decision": "allow"})
        self.feed.event("turn.ended")
        self.drain(key)
        discovery.write_json(folder / "keeper.json",
                             {**info, "swe_version": "0.0.1",
                              "state": "running"})
        rt.tick()
        self.assertTrue(self.finished(self.keepers[0]))
        self.assertTrue(rt.owns(key))
        self.assertEqual(self.store().get_session(key).state, "parked")
        # The next send starts a keeper on this version, which resumes.
        status, out = self.post(key, "message", {"text": "go on"})
        self.assertEqual((status, out["receipt"]), (202, "accepted"))
        self.feed.event("turn.ended")
        self.assertEqual(len(self.keepers), 2)
        start = self.calls("start", self.adapters[1])[0].args[0]
        self.assertEqual(start.resume_id, self.specs[0]["session_id"])
        seqs = self.seqs(key)
        self.assertEqual(seqs, sorted(set(seqs)))

    def test_with_the_setting_off_a_running_keeper_is_still_served(self):
        key = self.launch([fake.say("hi")], [fake.say("again")])
        self.feed.event("turn.ended")
        self.kill_hub(self.srv)
        self.feed = Feed(self)
        self.srv = self.hub(on=False)
        rt = self.srv.state.runtime
        self.assertTrue(rt.owns(key))
        self.assertFalse(rt.can_drive("claude"))
        self.assertEqual(self.post(key, "message", {"text": "more"})[0], 202)
        self.feed.event("turn.ended")


class TestAgentCrash(KeeperHubCase):
    def test_the_agent_dies_mid_turn_and_the_next_send_restarts_it(self):
        self.plans.append({"turns": [
            [fake.say("working"), fake.ask("go?", options=OPTIONS)]]})
        self.plans.append({"turns": [[fake.say("back")]]})
        key = self.launch()
        self.feed.event("approval.requested")
        stream = Stream(self, self.srv.state, key)
        globe = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        self.adapters[0].crash("claude exited with status 1")
        ended = self.feed.event("turn.ended")
        self.assertEqual(ended["data"]["status"], "failed")
        self.feed.event("session.state", state="agent_down")
        # Old clients: the card is retired and the status says error.
        gone = stream.frame("approval.resolved")
        self.assertEqual(gone["data"]["source"], "agent_exit")
        stream.frame("session.status", status="error")
        globe.frame("notify", kind="error")
        self.assertEqual(self.srv.state.pending_approvals, {})
        status, out = self.call("GET", f"/v1/sessions?key={key}")
        self.assertEqual(
            (out["sessions"][0]["status"], out["sessions"][0]["state"]),
            ("error", "agent_down"))
        self.assertEqual(self.store().get_session(key).state, "agent_down")

        status, out = self.post(key, "message", {"text": "are you there"})
        self.assertEqual((status, out["receipt"]), (202, "accepted"))
        self.feed.event("turn.ended", status="completed")
        self.assertEqual(len(self.adapters), 2)
        self.assertEqual(len(self.keepers), 1)
        # The new agent resumed the same harness session.
        start = self.calls("start", self.adapters[1])[0].args[0]
        self.assertEqual(start.resume_id, self.specs[0]["session_id"])
        self.assertEqual(self.store().get_session(key).state, "idle")

    def test_an_agent_that_keeps_dying_refuses_the_send(self):
        self.keeper_options = {"restart_tries": 1}
        self.plans.append({"turns": [[fake.exit_agent("boom")]]})
        self.plans.append({"turns": [[fake.exit_agent("boom again")]]})
        key = self.launch()
        self.feed.event("session.state", state="agent_down")
        status, out = self.post(key, "message", {"text": "one"})
        self.assertEqual(status, 202, out)
        self.feed.until(lambda e: e["type"] == "session.state"
                        and e["data"]["state"] == "agent_down"
                        and e["data"].get("reason") == "boom again",
                        "the second death")
        status, out = self.post(key, "message",
                                {"text": "two", "client_id": "m2"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "send_rejected"))
        self.assertIn("try again in", out["error"]["message"])
        self.assertEqual(self.store().get_command("m2").status, "rejected")


class TestKeeperLost(KeeperHubCase):
    def test_a_killed_keeper_is_replaced_on_the_next_send(self):
        self.plans.append({"turns": [
            [fake.say("working"), fake.ask("go?", options=OPTIONS)]]})
        self.plans.append({"turns": [[fake.say("resumed")]]})
        key = self.launch()
        asked = self.feed.event("approval.requested")
        self.drain(key)
        cut = self.seqs(key)[-1]
        stream = Stream(self, self.srv.state, key)
        kill(self.keepers[0])
        # Nothing is written to the timeline while no keeper holds it;
        # open streams are told with frames that carry no seq.
        gone = stream.frame("approval.resolved")
        self.assertEqual(
            (gone["data"]["request_id"], gone["data"]["source"],
             "seq" in gone),
            (asked["data"]["request_id"], "keeper_lost", False))
        status_frame = stream.frame("session.status", status="error")
        self.assertNotIn("seq", status_frame)
        self.assertEqual(self.seqs(key)[-1], cut)
        self.assertEqual(self.srv.state.pending_approvals, {})
        status, out = self.call("GET", f"/v1/sessions?key={key}")
        self.assertEqual(
            (out["sessions"][0]["status"], out["sessions"][0]["state"]),
            ("error", "agent_down"))
        self.assertEqual([k.state for k in self.store().list_keepers()],
                         ["lost"])

        status, out = self.post(key, "message",
                                {"text": "still there?", "client_id": "m2"})
        self.assertEqual((status, out["receipt"]), (202, "accepted"))
        self.feed.event("turn.ended", status="completed")
        self.assertEqual(len(self.keepers), 2)
        # The new keeper recorded what the old one left open, then
        # resumed the harness session by its own id.
        events = self.journal(key)
        kinds = [(e["type"], e["data"].get("status") or
                  e["data"].get("source")) for e in events
                 if e["seq"] > cut]
        self.assertEqual(kinds[:2], [("approval.resolved", "agent_exit"),
                                     ("turn.ended", "keeper_lost")])
        start = self.calls("start", self.adapters[1])[0].args[0]
        self.assertEqual(start.resume_id, self.specs[0]["session_id"])
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(self.store().get_session(key).state, "idle")
        self.assertEqual(sorted(k.state for k in self.store().list_keepers()),
                         ["live"])

    def test_a_message_lost_with_its_keeper_is_failed_not_resent(self):
        self.plans.append({"turns": [[fake.ask("go?", options=OPTIONS)]]})
        self.plans.append({"turns": []})
        key = self.launch()
        self.feed.event("approval.requested")
        store = self.store()
        # A send the keeper took (its receipt is in the log) and never
        # started before it died: known, no result, not in the queue.
        keeper = self.keepers[0]
        keeper.log.emit(ev.command_receipt(key, "m5", "accepted",
                                           op="send"))
        store.record_command(
            "m5", session=key, op="send", status="queued",
            payload={"text": "lost", "mode": "queue", "attachments": []},
            machine=self.srv.state.machine_id)
        kill(keeper)
        stream = Stream(self, self.srv.state, key)
        stream.frame("session.status", status="error")
        status, out = self.post(key, "message", {"text": "new"})
        self.assertEqual(status, 202, out)
        failed = stream.frame("command.result", client_id="m5")
        self.assertEqual((failed["data"]["ok"], failed["data"]["op"]),
                         (False, "message"))
        self.assertIn("send it again", failed["data"]["note"])
        self.assertFalse(store.get_command("m5").result["ok"])
        self.assertEqual([c.args[0].text for c in
                          self.calls("send_turn", self.adapters[1])],
                         ["new"])

    def test_a_keeper_found_dead_at_start_marks_the_turn_cut_off(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        self.feed.event("approval.requested")
        self.kill_hub(self.srv)
        kill(self.keepers[0])
        self.srv = self.hub()
        rt = self.srv.state.runtime
        self.assertTrue(rt.owns(key))
        self.assertTrue(rt.session(key).lost)
        status, out = self.call("GET", f"/v1/sessions?key={key}")
        self.assertEqual(out["sessions"][0]["status"], "error")
        self.assertEqual(self.srv.state.pending_approvals, {})


class TestContinueInSwe(KeeperHubCase):
    SID = "0f4d7a52-3a44-4c3e-9d6b-2f8a5f6f0001"

    def setUp(self) -> None:
        super().setUp()
        self.end(self.srv)
        self.key = f"claude:{self.SID}"
        self.session = _session(sid=self.SID, path=str(self.work), ts=100)
        self.enterContext(mock.patch.object(
            hub_mod.supervise, "_local_writer_pids", return_value=[]))
        self.srv = self.hub(sessions=[self.session],
                            statuses=lambda ss: ["done"] * len(ss))

    def history(self, n: int = 3) -> None:
        for i in range(n):
            self.srv.state.emit(self.key, protocol.event(
                "message.appended", self.key, self.srv.state.machine_id,
                protocol.message("assistant", f"old {i}")))

    def test_a_terminal_session_is_resumed_under_a_keeper(self):
        self.history(3)
        head = self.seqs(self.key)[-1]
        tm = self.srv.state.tail_manager
        self.assertIn(self.key, tm.tailers)
        self.plans.append({"turns": [[fake.say("continued")]]})
        stream = Stream(self, self.srv.state, self.key)
        status, out = self.post(self.key, "control", {"op": "attach"})
        self.assertEqual((status, out["note"]), (200, "continued in SWE"))
        # Resumed by its own id; the keeper's log starts where the
        # journal stood.
        spec = self.specs[0]
        self.assertEqual((spec["resume_id"], spec["first_seq"],
                          spec["session_id"]), (self.SID, head + 1, ""))
        start = self.calls("start")[0].args[0]
        self.assertEqual((start.resume_id, start.fork), (self.SID, False))
        own = stream.frame("session.ownership")
        self.assertEqual((own["data"]["ownership"], own["data"]["mode"],
                          "seq" in own), ("attached", "keeper", False))
        # The tailer is gone and stays gone.
        self.assertNotIn(self.key, tm.tailers)
        tm.sync(self.srv.state.catalog.tail_rows())
        tm.ensure(self.key, self.session, "done")
        self.assertNotIn(self.key, tm.tailers)
        # The old journal is the earlier history; the keeper's events
        # follow it with no seq used twice.
        status, out = self.post(self.key, "message", {"text": "go on"})
        self.assertEqual(status, 202, out)
        self.feed.event("turn.ended")
        events = self.journal(self.key)
        self.assertEqual([e["seq"] for e in events],
                         list(range(1, len(events) + 1)))
        self.assertEqual([e["v"] for e in events[:head + 1]],
                         [1] * head + [2])
        status, page = self.call(
            "GET", f"/v1/sessions/{self.key}/messages")
        self.assertEqual(
            [m["data"]["text"] for m in page["messages"]],
            ["old 0", "old 1", "old 2", "go on", "continued"])
        status, out = self.call("GET", f"/v1/sessions?key={self.key}")
        row = out["sessions"][0]
        self.assertEqual((row["ownership"], row["mode"], row["title"]),
                         ("attached", "keeper", "test"))
        self.assertEqual(self.store().get_session(self.key)
                         .harness_session_id, self.SID)

    def test_the_hubs_own_events_take_no_seq_from_the_keeper(self):
        self.history(2)
        self.plans.append({"turns": []})
        self.assertEqual(self.post(self.key, "control",
                                   {"op": "attach"})[0], 200)
        head = self.seqs(self.key)[-1]
        stream = Stream(self, self.srv.state, self.key)
        globe = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        status, out = self.post(self.key, "title", {"title": "Renamed"})
        self.assertEqual(status, 200, out)
        meta = stream.frame("session.meta")
        self.assertNotIn("seq", meta)
        self.assertIn("seq", globe.frame("session.meta"))
        self.assertEqual(self.seqs(self.key)[-1], head)

    def test_refused_while_another_process_has_the_session(self):
        with mock.patch.object(hub_mod.supervise, "_local_writer_pids",
                               return_value=[4242]):
            status, out = self.post(self.key, "control", {"op": "attach"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "session_in_use"))
        self.assertIn("4242", out["error"]["message"])
        self.assertEqual(self.keepers, [])
        self.assertFalse(self.srv.state.runtime.owns(self.key))

    def test_refused_while_the_session_is_busy_or_just_wrote(self):
        self.srv.state.catalog.patch(self.key, {"status": "active"},
                                     "active")
        status, out = self.post(self.key, "control", {"op": "attach"})
        self.assertEqual((status, out["error"]["code"]), (409, "not_idle"))
        self.srv.state.catalog.patch(self.key, {"status": "done"}, "done")
        transcript = self.work / "t.jsonl"
        transcript.write_text("{}\n")
        with mock.patch.object(hub_mod.tail, "resolve_jsonl_path",
                               return_value=transcript):
            status, out = self.post(self.key, "control", {"op": "attach"})
        self.assertEqual((status, out["error"]["code"]), (409, "not_idle"))
        self.assertIn("still moving", out["error"]["message"])
        self.assertEqual(self.keepers, [])

    def test_a_keeper_that_cannot_start_leaves_the_session_as_it_was(self):
        def no_keeper(key, **kw):
            raise discovery.SpawnError("no", "")
        self.srv.state.runtime.spawn = no_keeper
        self.history(1)
        head = self.seqs(self.key)[-1]
        status, out = self.post(self.key, "control", {"op": "attach"})
        self.assertEqual((status, out["error"]["code"]),
                         (503, "keeper_failed"))
        self.assertFalse(self.srv.state.runtime.owns(self.key))
        # Tailed again, and its events are journaled again.
        self.srv.state.emit(self.key, protocol.event(
            "message.appended", self.key, self.srv.state.machine_id,
            protocol.message("assistant", "later")))
        self.assertEqual(self.seqs(self.key)[-1], head + 1)

    def test_an_explicit_mode_still_asks_for_that_channel(self):
        with mock.patch.object(hub_mod.supervise, "attach",
                               return_value=(True, "attached (headless)")) \
                as attach:
            status, out = self.post(self.key, "control",
                                    {"op": "attach", "mode": "headless"})
        self.assertEqual((status, out["note"]),
                         (200, "attached (headless)"))
        attach.assert_called_once()
        self.assertEqual(self.keepers, [])

    def test_a_parked_session_resumed_in_a_terminal_goes_read_only(self):
        transcript = self.work / "session.jsonl"
        transcript.write_text("x" * 40)
        self.keeper_options = {"park_after": 60.0}
        self.plans.append({"turns": [], "position": TranscriptPosition(
            str(transcript), 40)})
        self.assertEqual(self.post(self.key, "control",
                                   {"op": "attach"})[0], 200)
        keeper = self.keepers[0]
        self.assertTrue(keeper.sync(WAIT))
        self.clock.advance(61)
        keeper.poke()
        self.assertTrue(keeper.sync(WAIT))
        self.clock.advance(61)
        keeper.poke()
        self.feed.event("session.state", state="parked")
        rt = self.srv.state.runtime
        # Nothing wrote: it stays ours.
        rt.tick()
        self.assertTrue(rt.owns(self.key))
        # Someone resumed it in a terminal: the transcript grew.
        with open(transcript, "a") as fh:
            fh.write("y" * 10)
        stream = Stream(self, self.srv.state, self.key)
        rt.tick()
        self.assertFalse(rt.owns(self.key))
        self.assertTrue(self.finished(keeper))
        stream.frame("session.ownership", ownership="observed")
        stored = self.store().get_session(self.key)
        self.assertEqual((stored.control, stored.state),
                         ("read_only", "closed"))
        self.assertIn("resumed in a terminal", stored.control_reason)
        self.srv.state.catalog.refresh()
        status, out = self.call("GET", f"/v1/sessions?key={self.key}")
        row = out["sessions"][0]
        self.assertEqual((row["ownership"], row["control"]),
                         ("observed", "read_only"))
        self.assertIn("resumed in a terminal", row["control_reason"])
        self.assertNotIn("mode", row)
        # The tailer picks the transcript up where the keeper left it.
        cursor = json.loads(tail._cursor_path(
            self.srv.state.tail_manager.cursors_dir, self.key).read_text())
        self.assertEqual((cursor["path"], cursor["offset"]),
                         (str(transcript), 40))
        # The note outlives a hub restart: it is in the store.
        self.kill_hub(self.srv)
        self.srv = self.hub(sessions=[self.session],
                            statuses=lambda ss: ["done"] * len(ss))
        rt = self.srv.state.runtime
        status, out = self.call("GET", f"/v1/sessions?key={self.key}")
        self.assertEqual(out["sessions"][0]["control"], "read_only")
        status, out = self.post(self.key, "message", {"text": "x"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "not_attached"))
        # Continuing it again clears the read-only note.
        with open(transcript, "a") as fh:
            fh.write("z")
        os.utime(transcript, (1, 1))
        self.plans.append({"turns": []})
        self.srv.state.catalog.refresh()
        head = self.seqs(self.key)[-1]
        self.assertEqual(self.post(self.key, "control",
                                   {"op": "attach"})[0], 200)
        self.assertIsNone(rt.read_only_reason(self.key))
        # The second keeper's log carries on past the journal, which
        # the hub wrote to in between (the ownership event).
        first = self.specs[1]["first_seq"]
        self.assertEqual(first, head + 1)
        kinds = {e["seq"]: (e["v"], e["type"])
                 for e in self.journal(self.key)}
        self.assertEqual(kinds[head][0], 1)
        self.assertEqual(kinds[first], (2, "session.caps"))
        seqs = self.seqs(self.key)
        self.assertEqual(seqs, sorted(set(seqs)))


class TestFork(KeeperHubCase):
    def test_a_fork_starts_a_keeper_that_forks_by_id(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        self.plans.append({"turns": [[fake.say("forked")]]})
        stream = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        status, out = self.post(key, "fork", {"prompt": "try another way"})
        self.assertEqual((status, out["note"]), (202, "forking"))
        launched = stream.frame("session.launched")
        new_key = launched["session"]
        self.assertNotEqual(new_key, key)
        spec = self.specs[1]
        self.assertEqual((spec["fork"], spec["resume_id"]),
                         (True, self.specs[0]["session_id"]))
        self.assertEqual(new_key, f"claude:{spec['session_id']}")
        self.feed.until(lambda e: e["type"] == "turn.ended"
                        and e["session"] == new_key, "the fork's turn")
        self.assertEqual(len(self.srv.state.runtime.rows()), 2)

    def test_without_the_fork_capability_the_old_path_answers(self):
        self.REGISTRY = registry_for(fork=False)
        self.end(self.srv)
        self.srv = self.hub(sessions=[_session(path=str(self.work))])
        with mock.patch("swe.remote.fork.shutil.which", return_value=None):
            status, out = self.post("claude:s1", "fork", {})
        self.assertEqual((status, out["error"]["code"]),
                         (422, "not_installed"))


class TestOldClients(KeeperHubCase):
    """What a client that predates v2 is sent, over the real routes."""

    def sse(self, key: str, last: int = 0):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=WAIT)
        self.addCleanup(conn.close)
        conn.request("GET", f"/v1/sessions/{key}/events?last={last}",
                     headers=self.auth())
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        return resp

    def frames(self, resp, until) -> list[dict]:
        """Read SSE frames until ``until(frame)``."""
        out = []
        while True:
            line = resp.readline()
            if not line:
                self.fail(f"stream ended; saw {[f['type'] for f in out]}")
            if line.startswith(b"data: "):
                frame = json.loads(line[6:])
                out.append(frame)
                if until(frame):
                    return out

    def test_the_timeline_arrives_as_v1_frames(self):
        staged = self.work / "shot.png"
        staged.write_bytes(b"png")
        with mock.patch.object(hub_mod.attachments, "resolve",
                               return_value=str(staged)):
            key = self.launch(
                [fake.think("hmm", duration_s=1.5),
                 fake.say("hi there", chunks=["hi ", "there"]),
                 fake.tool("Bash", input={"command": "ls"}, output="a b",
                           summary="Bash · ls"),
                 fake.ask("run rm?", kind="command", options=OPTIONS)],
                [fake.say("seen")])
            rid = self.feed.event("approval.requested")["data"][
                "request_id"]
            self.assertEqual(self.post(key, "approval", {
                "request_id": rid, "decision": "allow"})[0], 200)
            self.feed.event("turn.ended")
            status, out = self.post(key, "message", {
                "text": "look", "attachments": ["ref1"],
                "client_id": "m1"})
            self.assertEqual(status, 202, out)
            self.feed.result("m1")
            self.drain(key)
        resp = self.sse(key)
        frames = self.frames(
            resp, lambda f: f["type"] == "journal.caught-up")
        self.assertEqual({f.get("v", 1) for f in frames}, {1})
        types = [f["type"] for f in frames[:-1]]
        self.assertEqual(set(types), {
            "session.status", "message.appended", "approval.requested",
            "approval.resolved", "command.result"})
        # One v1 frame per v2 event at most, each keeping its seq.
        seqs = [f["seq"] for f in frames[:-1]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertLess(len(seqs), len(self.journal(key)))
        self.assertEqual(frames[-1]["seq"], self.seqs(key)[-1])
        kinds = [f["data"]["kind"] for f in frames
                 if f["type"] == "message.appended"]
        self.assertEqual(kinds, ["user", "thinking", "assistant", "tool_use",
                                 "tool_result", "user", "assistant"])
        texts = [f["data"]["text"] for f in frames
                 if f["type"] == "message.appended"
                 and f["data"]["kind"] == "user"]
        # Claude expands @path, so that is how the attachment reads.
        self.assertEqual(texts, ["hello", f"@{staged} look"])
        card = next(f for f in frames if f["type"] == "approval.requested")
        self.assertEqual(
            (card["data"]["request_id"], card["data"]["prompt"],
             [(o["n"], o["label"]) for o in card["data"]["options"]]),
            (rid, "run rm?", [("1", "Allow"), ("2", "Always"),
                              ("3", "Deny")]))
        self.assertTrue(card["data"]["options"][1]["standing"])
        statuses = [f["data"]["status"] for f in frames
                    if f["type"] == "session.status"]
        self.assertEqual(statuses[:4], ["done", "active", "followup",
                                        "active"])
        results = [f["data"] for f in frames
                   if f["type"] == "command.result"]
        # A send's result is named the way a v1 client matches it.
        mine = next(r for r in results if r["client_id"] == "m1")
        self.assertEqual((mine["op"], mine["ok"]), ("message", True))
        answered = next(r for r in results if r.get("request_id") == rid)
        self.assertEqual(answered["op"], "approval")

        # Live: the same frames, plus the text as it streams.
        self.plans[:] = []
        self.adapters[0].script.append([fake.say("live one",
                                                 chunks=["live ", "one"])])
        self.assertEqual(self.post(key, "message", {
            "text": "more", "client_id": "m2"})[0], 202)
        live = self.frames(
            resp, lambda f: f["type"] == "session.status"
            and f["data"]["status"] == "done")
        deltas = [f for f in live if f["type"] == "message.delta"]
        self.assertEqual([d["data"]["text"] for d in deltas],
                         ["live ", "one"])
        self.assertTrue(all("seq" not in d for d in deltas))
        self.assertIn("live one", [f["data"].get("text") for f in live
                                   if f["type"] == "message.appended"])
        # A reconnect from a cursor replays only what followed it.
        cursor = live[0].get("seq") or frames[-1]["seq"]
        again = self.frames(self.sse(key, last=cursor),
                            lambda f: f["type"] == "journal.caught-up")
        self.assertTrue(all(f["seq"] > cursor for f in again[:-1]))

    def test_a_question_with_choices_is_an_approval_card(self):
        rt = self.srv.state.runtime
        key = "claude:x"
        choice = ev.make("input.requested", key, {
            "request_id": "q1", "prompt": "Which branch?", "kind": "choice",
            "options": [{"id": "main", "label": "main"},
                        {"id": "dev", "label": "dev"}]}, seq=7, ts=5.0)
        card = rt.v1(key, choice)
        self.assertEqual(
            (card["v"], card["type"], card["seq"], card["data"]),
            (1, "approval.requested", 7, {
                "request_id": "q1", "prompt": "Which branch?",
                "options": [{"n": "1", "label": "main"},
                            {"n": "2", "label": "dev"}]}))
        # A free-text question has no v1 frame; the status still says
        # the session waits.
        text = ev.make("input.requested", key, {
            "request_id": "q2", "prompt": "Name it", "kind": "text"}, seq=8)
        self.assertIsNone(rt.v1(key, text))
        self.assertIsNone(rt.v1(key, ev.make(
            "turn.started", key, {}, turn_id="t1", seq=9)))
        v1 = protocol.event("message.appended", key, "m", {"kind": "user"})
        self.assertIs(rt.v1(key, v1), v1)

    def test_a_choice_is_answered_by_its_row_number(self):
        key = self.launch([fake.emit(
            "input.requested", request_id="q1", prompt="Which?",
            kind="choice", options=[{"id": "main", "label": "main"},
                                    {"id": "dev", "label": "dev"}])])
        self.feed.event("input.requested")
        self.assertEqual(self.srv.state.pending_approvals, {key: "q1"})
        status, out = self.post(key, "approval",
                                {"request_id": "q1", "decision": "2"})
        # The scripted agent never asked, so its answer fails; what
        # matters is that row 2 reached the keeper as that option.
        self.assertEqual(status, 200, out)
        self.feed.until(lambda e: e["type"] == "command.result"
                        and e["data"]["op"] == "respond", "the answer")
        answer = self.calls("respond")[0]
        self.assertEqual((answer.args[0], answer.args[1].option_id),
                         ("q1", "dev"))

    def test_a_steer_reaches_the_turn_in_flight(self):
        key = self.launch([fake.ask("go?", options=OPTIONS)])
        self.feed.event("approval.requested")
        status, out = self.post(key, "message", {
            "text": "wait, stop", "mode": "steer", "client_id": "s1"})
        self.assertEqual((status, out["receipt"], out["waiting"]),
                         (202, "accepted", False))
        self.feed.result("s1")
        self.assertEqual([c.args[0].text for c in self.calls("steer")],
                         ["wait, stop"])


class TestSettingOff(HubBase):
    def test_nothing_changes_without_the_setting(self):
        state = self.srv.state
        self.assertFalse(state.runtime.enabled)
        self.assertFalse(state.runtime.active)
        self.assertFalse(state.runtime.can_drive("claude"))
        self.assertFalse((self.srv.home / ".swe" / "data" / "remote" /
                          "remote.sqlite").exists())
        self.assertEqual(state.runtime.rows(), [])
        state.runtime.tick()
        # A session event is journaled as it always was.
        head = state.journal.last_seq("claude:s1")
        written = state.emit("claude:s1", protocol.event(
            "session.status", "claude:s1", state.machine_id,
            {"status": "done"}))
        self.assertEqual(written["seq"], head + 1)
        with mock.patch("swe.remote.catalog.launch_command",
                        return_value="claude"), \
                mock.patch("swe.remote.supervise.tmux_bin",
                           return_value=None):
            status, data = self.srv.request("POST", "/v1/sessions", {
                "harness": "claude", "cwd": str(self.srv.home),
                "prompt": "go"}, headers=self.auth())
        self.assertEqual(json.loads(data)["error"]["message"],
                         "tmux is not installed")

    def test_the_setting_is_read_from_config_json(self):
        home = Path(self.srv.home)
        self.assertFalse(runtime_mod.enabled(home))
        config = home / ".swe" / "config" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"remote": {"session_view": True}}))
        self.assertTrue(runtime_mod.enabled(home))
        config.write_text(json.dumps({"remote": {"session_view": "yes"}}))
        self.assertFalse(runtime_mod.enabled(home))
        config.write_text("not json")
        self.assertFalse(runtime_mod.enabled(home))

    def test_satellite_command_ids_never_repeat(self):
        state = self.srv.state
        self.srv.satellite("mini")
        ids = {state.queue_command("mini", "tail", "claude:a@mini", {})
               ["data"]["command_id"] for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(i.startswith("c") and len(i) == 25 for i in ids))


class TestJournalAt(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = Journal(short_home(self))

    def test_append_at_keeps_the_seq_it_is_given(self):
        j, key = self.journal, "claude:k"
        j.append(key, {"type": "old"})
        self.assertTrue(j.append_at(key, {"type": "a", "seq": 2}))
        self.assertTrue(j.append_at(key, {"type": "b", "seq": 5}))
        # A replayed event, or one from before, lands nowhere.
        self.assertFalse(j.append_at(key, {"type": "b", "seq": 5}))
        self.assertFalse(j.append_at(key, {"type": "x", "seq": 3}))
        self.assertEqual([(e["seq"], e["type"])
                          for e in j.read_since(key, 0)[0]],
                         [(1, "old"), (2, "a"), (5, "b")])
        self.assertEqual(j.append(key, {"type": "next"})["seq"], 6)
        # A fresh instance reads the same last seq from disk.
        self.assertEqual(Journal(j.dir.parent.parent.parent.parent)
                         .last_seq(key), 6)
        for bad in ({"type": "x"}, {"seq": 0}, {"seq": True},
                    {"seq": "7"}):
            with self.assertRaises(ValueError):
                j.append_at(key, bad)

    def test_an_append_that_cannot_be_written_is_not_counted(self):
        j, key = self.journal, "claude:k"
        with mock.patch("swe.remote.journal.os.open",
                        side_effect=OSError("disk full")):
            self.assertFalse(j.append_at(key, {"type": "a", "seq": 1}))
        self.assertEqual(j.last_seq(key), 0)
        self.assertTrue(j.append_at(key, {"type": "a", "seq": 1}))

    def test_drop_starts_the_numbering_over(self):
        j, key = self.journal, "claude:k"
        j.append_at(key, {"type": "a", "seq": 40})
        j.drop(key)
        j.drop(key)
        self.assertEqual(j.last_seq(key), 0)
        self.assertTrue(j.append_at(key, {"type": "a", "seq": 1}))
        with j.hold(key):
            self.assertEqual(j.append(key, {"type": "b"})["seq"], 2)


class TestCursorAheadOfTheKeeper(KeeperHubCase):
    def test_a_journal_from_another_log_is_started_over(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        self.kill_hub(self.srv)
        # The journal now claims more than the keeper's log holds: it
        # is not the log this journal followed.
        Journal(self.home).append_at(key, {"type": "stray", "seq": 900})
        self.feed = Feed(self)
        self.srv = self.hub()
        seqs = self.seqs(key)
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(self.journal(key),
                         read_log(key, home=self.home).events)


class TestCapabilitySeed(unittest.TestCase):
    def test_the_seed_is_what_the_fixtures_prove(self):
        from swe.remote.adapters import claude_wire

        seed = SESSION_SEED["claude"]
        caps = Caps(**{k: seed[k] for k in ("steer", "plan_mode", "rollback",
                                            "fork", "stop_task")},
                    options=frozenset(seed["options"]))
        self.assertEqual(caps, claude_wire.PROVEN)
        self.assertEqual(tuple(seed["argv"]), claude_wire.DEFAULT_ARGV)
        self.assertEqual(seed["min_version"], claude_wire.MIN_VERSION)
        resolved = registry.adapter_for(
            "claude", {"claude": {"capabilities": {"session": dict(seed)}}},
            which=None)
        self.assertEqual((resolved.ok, resolved.source, resolved.caps),
                         (True, "registry", claude_wire.PROVEN))

    def test_seeding_fills_an_entry_that_declares_nothing(self):
        saved = {}
        reg = {"claude": {"command": "claude",
                          "capabilities": {"model": {"flag": "--model"}}},
               "codex": {"command": "codex"}}
        hub = mock.Mock(home=None)
        rt = runtime_mod.SessionRuntime(hub, on=True)
        with mock.patch("swe.harness.registry.load_registry_if_present",
                        return_value=reg), \
                mock.patch("swe.harness.registry.save_registry",
                           side_effect=saved.update) as save:
            self.assertEqual(rt.seed_capabilities(), ["claude"])
            session = saved["claude"]["capabilities"]["session"]
            self.assertEqual(session, json.loads(json.dumps(
                dict(SESSION_SEED["claude"]))))
            self.assertEqual(saved["claude"]["capabilities"]["model"],
                             {"flag": "--model"})
            self.assertNotIn("capabilities", saved["codex"])
            # Once it declares one, it is never touched again.
            session["steer"] = False
            self.assertEqual(rt.seed_capabilities(), [])
            self.assertEqual(save.call_count, 1)
        # A harness that is not registered gets no entry made for it.
        with mock.patch("swe.harness.registry.load_registry_if_present",
                        return_value={}), \
                mock.patch("swe.harness.registry.save_registry") as save:
            self.assertEqual(rt.seed_capabilities(), [])
            save.assert_not_called()
        # An injected registry (tests) is never written.
        self.assertEqual(runtime_mod.SessionRuntime(
            hub, on=True, registry={}).seed_capabilities(), [])


class TestServiceUpgrade(unittest.TestCase):
    def setUp(self) -> None:
        self.home = short_home(self)
        self.path = launchd.plist_path(self.home)
        self.path.parent.mkdir(parents=True)

    def old_plist(self) -> dict:
        plist = launchd.build_plist(
            ["/Applications/SWE.app/swe", "remote", "serve", "--tailnet"],
            Path("/tmp/serve.log"), "/usr/bin:/opt/bin")
        del plist["AbandonProcessGroup"]
        plist["EnvironmentVariables"]["EXTRA"] = "kept"
        return plist

    def test_an_old_plist_gains_the_key_and_keeps_the_rest(self):
        self.assertFalse(launchd.upgrade_plist(self.home))
        old = self.old_plist()
        self.path.write_bytes(plistlib.dumps(old))
        self.assertIs(launchd.keeps_children(self.home), False)
        self.assertTrue(launchd.upgrade_plist(self.home))
        new = plistlib.loads(self.path.read_bytes())
        self.assertEqual(new, {**old, "AbandonProcessGroup": True})
        self.assertIs(launchd.keeps_children(self.home), True)
        self.assertFalse(launchd.upgrade_plist(self.home))
        self.path.write_bytes(b"not a plist")
        self.assertFalse(launchd.upgrade_plist(self.home))

    def test_the_running_job_reloads_itself_from_outside_the_job(self):
        from swe.remote import commands

        self.path.write_bytes(plistlib.dumps(self.old_plist()))
        started = []

        def popen(argv, **kw):
            started.append((argv, kw))

        with mock.patch.object(commands.sys, "platform", "darwin"):
            # Run from a shell: the file is fixed, nothing is reloaded.
            note = commands.upgrade_service(
                home=self.home, environ={"XPC_SERVICE_NAME": "0"},
                popen=popen)
            self.assertIn("next loads", note)
            self.assertEqual(started, [])
            self.assertEqual(commands.upgrade_service(
                home=self.home, environ={}, popen=popen), "")
            # Run as the job: reloaded by a shell in its own session.
            self.path.write_bytes(plistlib.dumps(self.old_plist()))
            note = commands.upgrade_service(
                home=self.home,
                environ={"XPC_SERVICE_NAME": launchd.LABEL}, popen=popen)
        self.assertIn("reloading", note)
        (argv, kw), = started
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertIn("launchctl bootout", argv[2])
        self.assertIn("launchctl bootstrap", argv[2])
        self.assertEqual(argv[4:], [f"gui/{os.getuid()}", launchd.LABEL,
                                    str(self.path)])
        self.assertIs(kw["start_new_session"], True)
        self.assertIs(launchd.keeps_children(self.home), True)

    def test_linux_has_nothing_to_change(self):
        from swe.remote import commands

        with mock.patch.object(commands.sys, "platform", "linux"):
            self.assertEqual(commands.upgrade_service(home=self.home), "")

    def test_the_reload_script_runs_in_a_shell(self):
        # The script itself, against a stand-in launchctl: bootout, then
        # bootstrap until it loads.
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        log = self.home / "calls"
        tries = self.home / "tries"
        (bin_dir / "launchctl").write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{log}"\n'
            'if [ "$1" = bootstrap ]; then\n'
            f'  echo x >> "{tries}"\n'
            f'  [ "$(wc -l < "{tries}")" -ge 2 ] || exit 5\n'
            "fi\nexit 0\n")
        os.chmod(bin_dir / "launchctl", 0o755)
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(bin_dir / "sleep", 0o755)
        done = subprocess.run(
            ["/bin/sh", "-c", launchd._RELOAD_SH, "sh", "gui/501", "label",
             "/p.plist"],
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, timeout=WAIT)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(log.read_text().splitlines(), [
            "bootout gui/501/label", "bootstrap gui/501 /p.plist",
            "bootstrap gui/501 /p.plist"])


class TestTailHandBack(unittest.TestCase):
    def test_skip_and_hand_back(self):
        home = short_home(self)
        emitted = []
        tm = tail.TailManager(lambda key, event: emitted.append(key), "m",
                              home=home)
        transcript = home / "t.jsonl"
        transcript.write_text('{"type":"x"}\n' * 3)
        session = _session(path=str(home))
        with mock.patch.object(tail, "make_source",
                               lambda s: tail.JsonlTailSource(transcript,
                                                              "claude")):
            tm.sync([("claude:s1", session, "done")])
            self.assertIn("claude:s1", tm.tailers)
            tm.skip = lambda key: key == "claude:s1"
            tm.sync([("claude:s1", session, "done")])
            self.assertEqual(tm.tailers, {})
            tm.ensure("claude:s1", session, "done")
            self.assertEqual(tm.tailers, {})
            # Handed back at a mark: the next tailer reads on from it.
            tm.skip = lambda key: False
            tm.hand_back("claude:s1", {"path": str(transcript), "offset": 13})
            tm.sync([("claude:s1", session, "done")])
            self.assertEqual(tm.tailers["claude:s1"].source.offset, 13)
            # Handed back with no mark: no cursor, so a tailer that
            # finds a journal starts at the end.
            tm.drop("claude:s1")
            tm.hand_back("claude:s1")
            self.assertFalse(tail._cursor_path(tm.cursors_dir,
                                               "claude:s1").exists())
            tm._journal_has = lambda key: True
            tm.sync([("claude:s1", session, "done")])
            self.assertEqual(tm.tailers["claude:s1"].source.offset,
                             transcript.stat().st_size)


class TestCatalogOverlay(unittest.TestCase):
    def owned(self, **over) -> dict:
        row = {"key": "claude:abc", "harness": "claude",
               "harness_session_id": "abc", "cwd": "/w/proj",
               "state": "running", "status": "active",
               "adapter": "claude_stream",
               "caps": {"fork": True, "options": ["model"]},
               "title": "", "active_at": 50.0, "model": "opus"}
        row.update(over)
        return row

    def test_a_session_with_a_transcript_is_one_row(self):
        session = _session(sid="abc", path="/w/proj", ts=10)
        key, row = catalog_mod.session_to_row(session, "done", "m", {})
        row["tmux"] = "swe-1"
        other = catalog_mod.session_to_row(_session(sid="zzz"), "done",
                                           "m", {})
        rows = catalog_mod.overlay_owned(
            [(key, session, row, "done"), (other[0], _session(sid="zzz"),
                                           other[1], "done")],
            [self.owned(key="claude:hubkey")], "m")
        self.assertEqual([r[0] for r in rows], ["claude:hubkey", other[0]])
        got = rows[0][2]
        self.assertEqual(
            (got["key"], got["ownership"], got["mode"], got["status"],
             got["state"], got["control"], got["can_fork"], got["model"],
             got["last_active_at"], rows[0][3]),
            ("claude:hubkey", "attached", "keeper", "active", "running",
             "full", True, "opus", 50.0, "active"))
        self.assertNotIn("tmux", got)
        self.assertEqual(got["model_modes"].count("keeper"), 1)
        # The aggregator's row was not changed in place.
        self.assertEqual(row["ownership"], "observed")

    def test_a_session_with_no_transcript_gets_a_row(self):
        rows = catalog_mod.overlay_owned(
            [], [self.owned(caps={}, model="")], "m")
        (key, session, row, status), = rows
        self.assertEqual(
            (key, session.session_id, row["title"], row["cwd"],
             row["can_fork"], row["attachable"], status),
            ("claude:abc", "abc", "proj", "/w/proj", False, True, "active"))
        self.assertNotIn("keeper", row["model_modes"])
        self.assertEqual(catalog_mod.overlay_owned([], [], "m"), [])


class RealProcess(HubOps, HubBase):
    """The hub over real keeper processes driving the fake ``claude``:
    the path ``swe remote serve`` takes."""

    def setUp(self) -> None:
        self.home = short_home(self)
        self.work = self.home / "work"
        self.work.mkdir()
        self.procs: list[subprocess.Popen] = []
        self.hubs = []
        self.addCleanup(self.reap)
        self.registry = {"claude": {"capabilities": {"session": {
            "adapter": "claude_stream",
            "argv": [sys.executable, str(FAKE_CLAUDE)],
            "steer": True, "plan_mode": True, "fork": True,
            "stop_task": True, "options": ["model", "permission_mode"]}}}}
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "harness.json").write_text(json.dumps(self.registry))
        self.enterContext(mock.patch(
            "swe.remote.catalog.launch_command", lambda harness: "claude"))
        self.feed = Feed(self)
        self.srv = self.hub()

    def popen(self, *args, **kw) -> subprocess.Popen:
        proc = subprocess.Popen(*args, **kw)
        self.procs.append(proc)
        return proc

    def reap(self) -> None:
        self.quiet_hubs()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(WAIT)

    def spawn(self, key, home=None, env=None, runtime=None):
        return discovery.spawn(
            key, home=home, runtime=runtime, popen=self.popen,
            scope=lambda slug: None,
            env={**(env or {}), "FAKE_CLAUDE_MODE": "approval",
                 "PYTHONPATH": str(SRC)})

    @property
    def REGISTRY(self) -> dict:
        return self.registry

    def test_launch_send_kill_the_hub_restart_and_approve(self):
        stream = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        status, out = self.call("POST", "/v1/sessions", {
            "harness": "claude", "cwd": str(self.work), "prompt": "hello"})
        self.assertEqual(status, 202, out)
        key = stream.frame("session.launched")["session"]
        asked = self.feed.event("approval.requested")
        self.drain(key)
        pid = self.procs[0].pid
        self.assertNotEqual(pid, os.getpid())
        status, out = self.post(key, "message",
                                {"text": "second", "client_id": "m2"})
        self.assertEqual(out["receipt"], "queued")
        self.feed.event("queue.updated")
        cut = self.seqs(key)[-1]

        self.kill_hub(self.srv)
        self.assertIsNone(self.procs[0].poll())
        self.feed = Feed(self)
        self.srv = self.hub()
        self.assertEqual(len(self.procs), 1)
        rid = asked["data"]["request_id"]
        self.assertEqual(self.srv.state.pending_approvals, {key: rid})
        status, out = self.post(key, "approval", {
            "request_id": rid, "decision": "allow", "client_id": "ok"})
        self.assertEqual((status, out["receipt"]), (200, "accepted"))
        self.feed.event("turn.ended", status="completed")
        # The queued message starts the next turn, which asks again.
        second = self.feed.event("approval.requested")
        status, out = self.post(key, "approval", {
            "request_id": second["data"]["request_id"],
            "decision": "allow"})
        self.assertEqual(status, 200, out)
        self.feed.event("turn.ended", status="completed")
        seqs = self.seqs(key)
        self.assertEqual(seqs, list(range(1, seqs[-1] + 1)))
        self.assertGreater(seqs[-1], cut)
        status, page = self.call("GET", f"/v1/sessions/{key}/messages")
        kinds = [(m["data"]["kind"], m["data"]["text"])
                 for m in page["messages"]
                 if m["data"]["kind"] in ("user", "assistant")]
        self.assertEqual(kinds, [("user", "hello"), ("assistant", "hello"),
                                 ("user", "second"),
                                 ("assistant", "hello")])
        # Detach ends the process cleanly.
        status, out = self.post(key, "control", {"op": "detach"})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.procs[0].wait(WAIT), discovery.EXIT_OK)

    def test_a_keeper_killed_by_a_signal_is_replaced(self):
        stream = Stream(self, self.srv.state, hub_mod.GLOBAL_KEY)
        status, out = self.call("POST", "/v1/sessions", {
            "harness": "claude", "cwd": str(self.work), "prompt": "hello"})
        key = stream.frame("session.launched")["session"]
        self.feed.event("approval.requested")
        watch = Stream(self, self.srv.state, key)
        os.kill(self.procs[0].pid, signal.SIGKILL)
        self.procs[0].wait(WAIT)
        watch.frame("session.status", status="error")
        status, out = self.post(key, "message", {"text": "again"})
        self.assertEqual(status, 202, out)
        self.feed.event("turn.ended", status="keeper_lost")
        self.feed.event("approval.requested")
        self.assertEqual(len(self.procs), 2)
        self.srv.state.runtime.stop_keepers()
        self.assertEqual(self.procs[1].wait(WAIT), discovery.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
