"""The session keeper (remote/keeper): the process, its IPC, discovery
and the service-manager pieces that let it outlive the hub.

Most tests run a ``Keeper`` on a thread of this process with the
scripted ``FakeAdapter`` and a ``FakeClock``, and talk to it over its
real Unix socket. Nothing sleeps: a test waits on a frame from the
socket or on ``keeper.sync()``. ``kill`` ends a keeper the way SIGKILL
does. ``RealProcess`` starts ``python -m swe.remote.keeper`` for real,
driving the fake ``claude`` under tests/fakes, and kills it with a
signal. Every test stops its keepers in cleanup.
"""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from swe.remote import launchd, systemd
from swe.remote.adapters import events as ev
from swe.remote.adapters import fake
from swe.remote.adapters.base import AdapterError, Caps, StartSpec
from swe.remote.keeper import discovery, ipc
from swe.remote.keeper import process as keeper_process
from swe.remote.keeper.log import EventLog, Replay
from swe.remote.keeper.process import Keeper
from swe.remote.protocol import key_slug

S = "claude:abc"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "fake_claude.py"
SRC = Path(__file__).resolve().parent.parent / "src"
WAIT = 20.0


def short_home(case: unittest.TestCase) -> Path:
    """A temp folder with a short path: a Unix socket's address is
    limited to about 100 bytes, and macOS's default temp folder alone
    is half of that."""
    base = tempfile.gettempdir()
    if len(base) > 30:
        base = "/tmp"
    return Path(os.path.realpath(case.enterContext(
        tempfile.TemporaryDirectory(dir=base, prefix="swk",
                                    ignore_cleanup_errors=True))))


def kill(keeper: Keeper) -> None:
    """End ``keeper`` the way SIGKILL does: nothing is closed in order.
    The agent is not told, the log gets no clean shutdown, keeper.json
    still says it runs; only what the kernel does at exit happens (the
    sockets and the lock go)."""
    keeper._shutdown = lambda: None  # the loop ends without tidying up
    keeper.stop()
    with keeper._conns_lock:
        conns = list(keeper._conns)
    for conn in conns:
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    keeper._wake_w.send(b"x")
    keeper._accept_thread.join(WAIT)
    for sock in (keeper._listener, keeper._wake_r, keeper._wake_w):
        sock.close()
    log = keeper.log
    with log._lock:
        log._closed = True
        log._release()
    os.close(keeper._lock_fd)
    keeper._lock_fd = None


class Feed:
    """One client's frames, read in order."""

    def __init__(self, case: unittest.TestCase,
                 client: ipc.KeeperClient) -> None:
        self.case = case
        self.client = client
        self.frames: list[dict] = []

    def until(self, match, what: str = "a frame") -> dict:
        """Read until ``match(frame)``; fail when none comes."""
        while True:
            frame = self.client.next(WAIT)
            if frame is None:
                self.case.fail(f"timed out waiting for {what}; saw "
                               f"{[self.label(f) for f in self.frames]}")
            self.frames.append(frame)
            if match(frame):
                return frame
            if frame["t"] == "closed":
                self.case.fail(f"connection closed waiting for {what}; saw "
                               f"{[self.label(f) for f in self.frames]}")

    @staticmethod
    def label(frame: dict) -> str:
        if frame.get("t") == "event":
            return frame["e"]["type"]
        return str(frame.get("t"))

    def event(self, etype: str, **data) -> dict:
        """Read until an event of ``etype`` whose data has ``data``."""
        def match(frame: dict) -> bool:
            if frame["t"] != "event" or frame["e"]["type"] != etype:
                return False
            return all(frame["e"]["data"].get(k) == v
                       for k, v in data.items())
        return self.until(match, f"{etype} {data}")["e"]

    def result(self, command_id: str) -> dict:
        """Read until the ``command.result`` of ``command_id``."""
        return self.until(
            lambda f: f["t"] == "event"
            and f["e"]["type"] == "command.result"
            and f["e"].get("command_id") == command_id,
            f"the result of {command_id}")["e"]

    def receipt(self, command_id: str) -> dict:
        """Read until the ``command.receipt`` of ``command_id``."""
        return self.until(
            lambda f: f["t"] == "event"
            and f["e"]["type"] == "command.receipt"
            and f["e"].get("command_id") == command_id,
            f"the receipt of {command_id}")["e"]

    def live(self) -> dict:
        return self.until(lambda f: f["t"] == "live", "the end of replay")

    def closed(self) -> None:
        self.until(lambda f: f["t"] == "closed", "the connection to close")

    def events(self, etype: str | None = None) -> list[dict]:
        return [f["e"] for f in self.frames if f["t"] == "event"
                and (etype is None or f["e"]["type"] == etype)]

    def seqs(self) -> list[int]:
        return [e["seq"] for e in self.events() if "seq" in e]


class KeeperCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = short_home(self)
        self.runtime = self.home / "rt"
        self.clock = fake.FakeClock()
        self.adapters: list[fake.FakeAdapter] = []
        # One dict of FakeAdapter options per agent start, in order.
        self.plans: list[dict] = []
        self._n = 0

    def factory(self) -> fake.FakeAdapter:
        opts = dict(self.plans.pop(0)) if self.plans else {}
        adapter = fake.FakeAdapter(clock=self.clock, **opts)
        self.adapters.append(adapter)
        return adapter

    def keeper(self, *plans: dict, start: bool = True, session: str = S,
               **kw) -> Keeper:
        self.plans.extend(plans)
        log_options = {"fsync": lambda fd: None, "background": False,
                       "boot_id": "boot-a"}
        log_options.update(kw.pop("log_options", {}))
        kw.setdefault("home", self.home)
        kw.setdefault("runtime", self.runtime)
        kw.setdefault("clock", self.clock)
        spec = kw.pop("spec", None) or StartSpec(session, "/tmp",
                                                 harness="claude")
        keeper = Keeper(session, spec, self.factory,
                        log_options=log_options, **kw)
        if start:
            keeper.start()
            self.addCleanup(self.stop, keeper)
            self.assertTrue(keeper.sync(WAIT))
        return keeper

    def stop(self, keeper: Keeper) -> None:
        if keeper._lock_fd is not None and not keeper.done:
            keeper.stop()

    def connect(self, keeper: Keeper, last_seq: int | None = 0,
                **kw) -> Feed:
        client = ipc.KeeperClient(keeper.socket_path, last_seq=last_seq,
                                  **kw)
        self.addCleanup(client.close)
        client.connect()
        return Feed(self, client)

    def cid(self) -> str:
        self._n += 1
        return f"c{self._n}"

    def send(self, feed: Feed, text: str = "hello", **kw) -> dict:
        kw.setdefault("command_id", self.cid())
        return feed.client.request("send", text=text, **kw)

    def status(self, feed: Feed) -> dict:
        return feed.client.request("status")


# ---------------------------------------------------------------------


class TestHandshake(KeeperCase):
    def test_replay_from_disk_then_live_without_a_gap(self):
        keeper = self.keeper({"turns": [[fake.say("one")],
                                        [fake.say("two")]]})
        first = self.connect(keeper)
        first.live()
        self.assertEqual(self.send(first)["status"], "accepted")
        first.event("command.result", op="send")
        first.client.close()

        feed = self.connect(keeper)
        welcome = feed.client.welcome
        self.assertEqual(welcome["v"], ipc.VERSION)
        self.assertEqual(welcome["session"], S)
        self.assertEqual(welcome["state"], "idle")
        self.assertEqual(welcome["agent"], "up")
        self.assertEqual(welcome["harness_session_id"], "fake-session")
        self.assertEqual(welcome["pending"], [])
        self.assertEqual(welcome["live"], [])
        marker = feed.live()
        self.assertEqual(marker["last_seq"], welcome["last_seq"])
        # The replay is everything on disk, in order, and nothing else.
        self.assertEqual(feed.seqs(), list(range(1, welcome["last_seq"] + 1)))
        self.assertNotIn("message.delta", [e["type"] for e in feed.events()])

        self.send(feed, "again")
        feed.event("command.result", op="send")
        seqs = feed.seqs()
        self.assertEqual(seqs, list(range(1, seqs[-1] + 1)))
        self.assertGreater(seqs[-1], welcome["last_seq"])
        # Live text reaches a connected client.
        self.assertTrue(feed.events("message.delta"))

    def test_cursor_replays_only_what_is_missing(self):
        keeper = self.keeper({"turns": [[fake.say("one")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("command.result", op="send")
        last = feed.seqs()[-1]
        behind = self.connect(keeper, last_seq=last - 3)
        behind.live()
        self.assertEqual(behind.seqs(), [last - 2, last - 1, last])
        tail = self.connect(keeper, last_seq=None)
        self.assertEqual(tail.live()["last_seq"], last)
        self.assertEqual(tail.seqs(), [])

    def test_join_mid_reply_gets_partial_text_and_pending_request(self):
        keeper = self.keeper({"autoplay": False, "turns": [[
            fake.emit("message.delta", id="m9", kind="assistant",
                      text="Hel", seq=1, offset=0),
            fake.ask("run ls?"),
            fake.say("done"),
        ]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("message.completed", kind="user")
        adapter = self.adapters[0]
        adapter.step()
        adapter.step()
        feed.event("approval.requested")

        late = self.connect(keeper, last_seq=None)
        welcome = late.client.welcome
        self.assertEqual(welcome["live"], [
            {"id": "m9", "kind": "assistant", "text": "Hel",
             "turn_id": "t1"}])
        self.assertEqual([p["data"]["request_id"]
                          for p in welcome["pending"]], ["r1"])
        self.assertEqual(welcome["open_turn"], "t1")
        self.assertEqual(welcome["state"], "waiting")

    def test_page_by_page_replay(self):
        keeper = self.keeper({"turns": [[fake.tool("ls", output=str(i))
                                         for i in range(20)]]},
                             replay_page=3)
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("command.result", op="send")
        again = self.connect(keeper)
        last = again.live()["last_seq"]
        self.assertEqual(again.seqs(), list(range(1, last + 1)))
        self.assertGreater(last, 40)


class TestVersions(KeeperCase):
    def test_negotiate(self):
        self.assertEqual(ipc.negotiate(1, 1, 1, 1), 1)
        self.assertEqual(ipc.negotiate(9, 1, 3, 2), 3)   # a newer client
        self.assertEqual(ipc.negotiate(2, 1, 3, 2), 2)   # an older client
        self.assertEqual(ipc.negotiate(2, None, 3, 2), 2)
        self.assertIsNone(ipc.negotiate(1, 1, 3, 2))     # too old
        self.assertIsNone(ipc.negotiate(9, 5, 3, 2))     # too new
        self.assertIsNone(ipc.negotiate("1", 1, 1, 1))
        self.assertIsNone(ipc.negotiate(True, 1, 1, 1))
        self.assertIsNone(ipc.negotiate(1, 2, 3, 1))     # min above v

    def test_an_older_client_is_spoken_to_in_its_version(self):
        keeper = self.keeper({"turns": [[fake.say("hi")]]},
                             ipc_version=3, ipc_min=2)
        feed = self.connect(keeper, version=2, min_version=2)
        self.assertEqual(feed.client.version, 2)
        self.assertEqual(feed.client.welcome["keeper_v"], 3)
        feed.live()
        self.assertEqual(self.send(feed)["status"], "accepted")
        feed.event("turn.ended", status="completed")

    def test_a_client_older_than_the_floor_is_refused(self):
        keeper = self.keeper(ipc_version=3, ipc_min=2)
        client = ipc.KeeperClient(keeper.socket_path, version=1,
                                  min_version=1)
        with self.assertRaises(ipc.Refused) as ctx:
            client.connect()
        self.assertEqual(ctx.exception.reason, "version")
        self.assertEqual(ctx.exception.frame["v"], 3)
        self.assertEqual(ctx.exception.frame["min_v"], 2)
        # The keeper is fine: the next client gets in.
        self.connect(keeper, version=3, min_version=2).live()

    def test_a_newer_hub_speaks_an_older_keepers_version(self):
        # After a reinstall the hub is newer than the keepers it finds.
        keeper = self.keeper(ipc_version=1, ipc_min=1)
        feed = self.connect(keeper, version=4, min_version=1)
        self.assertEqual(feed.client.version, 1)
        self.assertEqual(feed.client.welcome["keeper_v"], 1)

    def test_unknown_frames_fields_and_ops_are_tolerated(self):
        keeper = self.keeper({"turns": [[fake.say("hi")]]})
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.connect(str(keeper.socket_path))
        reader = ipc.LineReader(sock)
        ipc.send_frame(sock, {"t": "hello", "v": 1, "min_v": 1,
                              "last_seq": None, "shiny": {"new": True}})
        self.assertEqual(reader.read(WAIT)["t"], "welcome")
        self.assertEqual(reader.read(WAIT)["t"], "live")
        sock.sendall(b"\n")                       # a blank line
        ipc.send_frame(sock, {"t": "telemetry", "x": 1})
        ipc.send_frame(sock, {"t": "cmd", "id": 1, "op": "ping"})
        self.assertEqual(reader.read(WAIT),
                         {"t": "reply", "id": 1, "ok": True, "op": "ping"})
        ipc.send_frame(sock, {"t": "cmd", "id": 2, "op": "teleport",
                              "command_id": "x1"})
        frames = [reader.read(WAIT), reader.read(WAIT)]
        reply = [f for f in frames if f["t"] == "reply"][0]
        self.assertEqual((reply["status"], reply["reason"]),
                         ("rejected", "unknown_op"))

    def test_a_bad_hello_is_refused(self):
        keeper = self.keeper()
        for hello in ({"t": "cmd", "op": "ping"},
                      {"t": "hello", "v": 1, "last_seq": -4},
                      {"t": "hello", "v": 1, "last_seq": "7"}):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.connect(str(keeper.socket_path))
            ipc.send_frame(sock, hello)
            reader = ipc.LineReader(sock)
            self.assertEqual(reader.read(WAIT)["reason"], "bad_hello")
            self.assertIsNone(reader.read(WAIT))

    def test_a_line_that_is_not_json_ends_that_connection_only(self):
        keeper = self.keeper()
        feed = self.connect(keeper)
        feed.live()
        feed.client._sock.sendall(b"this is not json\n")
        feed.closed()
        self.assertTrue(feed.client.closed)
        with self.assertRaises(ipc.IpcError):
            feed.client.request("status")
        self.connect(keeper).live()


class TestCommands(KeeperCase):
    def test_a_command_is_logged_before_it_runs_and_never_twice(self):
        keeper = self.keeper({"turns": [[fake.say("one")]]})
        feed = self.connect(keeper)
        feed.live()
        reply = self.send(feed, command_id="c-1", device="phone",
                          attachments=["/w/a.png"])
        self.assertEqual(reply, {"t": "reply", "id": 1,
                                 "status": "accepted", "op": "send",
                                 "command_id": "c-1"})
        feed.event("command.result", op="send", ok=True)
        types = [e["type"] for e in feed.events()]
        self.assertLess(types.index("command.receipt"),
                        types.index("turn.started"))
        user = feed.events("message.completed")[0]
        self.assertEqual(user["data"]["by"], "phone")
        self.assertEqual(user["data"]["attachments"], ["/w/a.png"])

        again = self.send(feed, command_id="c-1")
        self.assertEqual((again["status"], again["first"]),
                         ("duplicate", "accepted"))
        known = feed.client.request("has_command", command_id="c-1")
        self.assertEqual((known["known"], known["status"]),
                         (True, "accepted"))
        self.assertFalse(feed.client.request(
            "has_command", command_id="nope")["known"])
        sends = [c for c in self.adapters[0].calls if c.name == "send_turn"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(feed.events("command.receipt")), 1)

    def test_bad_requests_are_rejected(self):
        keeper = self.keeper()
        feed = self.connect(keeper)
        feed.live()
        no_id = feed.client.request("send", text="hi")
        self.assertEqual(no_id["status"], "rejected")
        self.assertIn("command_id", no_id["reason"])
        for bad in ({"text": "  "}, {"text": "hi", "attachments": "a"},
                    {"text": "hi", "mode": "shout"},
                    {"text": "hi", "device": 7}):
            reply = feed.client.request("send", command_id=self.cid(), **bad)
            self.assertEqual((reply["status"], reply["reason"]),
                             ("rejected", "bad_request"), bad)
        self.assertEqual(self.adapters[0].calls[1:], [])
        # A rejection is on the record too.
        self.assertEqual(
            feed.client.request("has_command", command_id="c1")["status"],
            "rejected")

    def test_mid_turn_sends_queue_and_run_in_order(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")], [fake.say("b")],
                                        [fake.say("c")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.assertEqual(self.send(feed, "b", command_id="b",
                                   device="mac")["status"], "queued")
        self.assertEqual(self.send(feed, "c", command_id="c")["status"],
                         "queued")
        queue = feed.event("queue.updated")
        self.assertEqual(queue["data"]["items"], [
            {"command_id": "b", "text": "b", "by": "mac", "mode": "queue"}])
        self.assertEqual([i["command_id"]
                          for i in self.status(feed)["queue"]], ["b", "c"])
        # A client that joins now is told the queue.
        self.assertEqual([i["command_id"] for i in self.connect(
            keeper, last_seq=None).client.welcome["queue"]], ["b", "c"])

        feed.client.request("respond", command_id="ok", request_id="r1",
                            decision="allow")
        feed.event("command.result", op="send", ok=True)
        feed.result("c")
        started = [e["command_id"] for e in feed.events("turn.started")]
        self.assertEqual(started, ["a", "b", "c"])
        self.assertEqual(feed.events("queue.updated")[-1]["data"]["items"],
                         [])

    def test_queue_edit_and_remove(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")], [fake.say("x")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        self.send(feed, "c", command_id="c")
        req = feed.client.request
        self.assertEqual(req("queue_edit", command_id="e1", target="b",
                             text="b, but better")["status"], "accepted")
        self.assertEqual(req("queue_remove", command_id="e2",
                             target="c")["status"], "accepted")
        gone = feed.result("c")
        self.assertEqual((gone["data"]["ok"], gone["data"]["cancelled"]),
                         (False, True))
        self.assertEqual(self.status(feed)["queue"], [
            {"command_id": "b", "text": "b, but better", "mode": "queue"}])
        for op, extra in (("queue_edit", {"target": "zz", "text": "t"}),
                          ("queue_remove", {"target": "zz"})):
            reply = req(op, command_id=self.cid(), **extra)
            self.assertEqual((reply["status"], reply["reason"]),
                             ("rejected", "not_queued"))
        self.assertEqual(req("queue_edit", command_id=self.cid(),
                             target="b", text="")["reason"], "bad_request")

    def test_interrupt_and_clearing_the_queue(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")], [fake.ask("2?")],
                                        [fake.say("z")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        self.assertEqual(feed.client.request(
            "interrupt", command_id="i1")["status"], "accepted")
        feed.event("turn.ended", status="interrupted")
        # The queue runs on after a plain interrupt.
        feed.event("approval.requested", prompt="2?")
        self.send(feed, "c", command_id="c")
        feed.client.request("interrupt", command_id="i2", clear_queue=True)
        feed.event("command.result", op="interrupt")
        self.assertEqual(self.status(feed)["queue"], [])
        cancelled = [e for e in feed.events("command.result")
                     if e["data"].get("cancelled")]
        self.assertEqual([e["command_id"] for e in cancelled], ["c"])
        self.assertEqual([e["command_id"]
                          for e in feed.events("turn.started")], ["a", "b"])

    def test_steer_goes_to_the_turn_only_where_the_adapter_can(self):
        keeper = self.keeper({"caps": Caps(steer=True),
                              "turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a")
        feed.event("approval.requested")
        self.assertEqual(self.send(feed, "also this", command_id="s1",
                                   mode="steer")["status"], "accepted")
        feed.event("command.result", op="send", ok=True)
        steers = [c for c in self.adapters[0].calls if c.name == "steer"]
        self.assertEqual(steers[0].args[0].text, "also this")
        self.assertEqual(self.status(feed)["queue"], [])

    def test_steer_queues_where_the_adapter_cannot(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a")
        feed.event("approval.requested")
        self.assertEqual(self.send(feed, "also this",
                                   mode="steer")["status"], "queued")
        self.assertEqual([c.name for c in self.adapters[0].calls
                          if c.name == "steer"], [])

    def test_options_are_gated_by_caps_and_kept_for_the_next_start(self):
        caps = Caps(plan_mode=True, options=frozenset({"model"}))
        keeper = self.keeper({"caps": caps}, {"caps": caps})
        feed = self.connect(keeper)
        feed.live()
        req = feed.client.request
        self.assertEqual(req("set_option", command_id=self.cid(),
                             name="model", value="opus")["status"],
                         "accepted")
        self.assertEqual(req("set_option", command_id=self.cid(),
                             name="plan_mode", value=True)["status"],
                         "accepted")
        self.assertEqual(req("set_option", command_id=self.cid(),
                             name="effort", value="high")["reason"],
                         "unsupported")
        for name, value in (("model", "two words"), ("colour", "red"),
                            ("plan_mode", "yes"), (["x"], 1)):
            reply = req("set_option", command_id=self.cid(), name=name,
                        value=value)
            self.assertEqual(reply["status"], "rejected", (name, value))
        self.assertEqual(self.adapters[0].options,
                         {"model": "opus", "plan_mode": True})

        self.assertEqual(req("park", command_id=self.cid())["status"],
                         "accepted")
        feed.event("session.state", state="parked")
        # With no agent, the option waits for the next start.
        req("set_option", command_id="late", name="model", value="haiku")
        note = feed.result("late")
        self.assertIn("when the agent starts", note["data"]["note"])
        self.send(feed)
        feed.event("turn.ended")
        spec = self.adapters[1].spec
        self.assertEqual((spec.model, spec.plan_mode, spec.resume_id),
                         ("haiku", True, "fake-session"))

    def test_stop_task_and_rollback_follow_caps(self):
        keeper = self.keeper({"turns": [[fake.task("task-1"),
                                         fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        req = feed.client.request
        for op, field in (("stop_task", "task_id"), ("rollback", "turn_id")):
            reply = req(op, command_id=self.cid(), **{field: "x"})
            self.assertEqual((reply["status"], reply["reason"]),
                             ("rejected", "unsupported"))

    def test_stop_task_and_rollback_reach_the_adapter(self):
        caps = Caps(stop_task=True, rollback=True)
        keeper = self.keeper({"caps": caps, "turns": [
            [fake.say("one")], [fake.task("task-1"), fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        req = feed.client.request
        self.send(feed, "one")
        feed.event("turn.ended")
        self.assertEqual(req("rollback", command_id=self.cid(),
                             turn_id="t1")["status"], "accepted")
        feed.event("command.result", op="rollback", ok=True)
        self.assertEqual(req("rollback", command_id="rb2",
                             turn_id="t9")["status"], "accepted")
        failed = feed.event("command.result", op="rollback", ok=False)
        self.assertIn("t9", failed["data"]["note"])
        self.assertEqual(req("stop_task", command_id=self.cid(),
                             task_id="")["reason"], "bad_request")
        self.send(feed, "two")
        feed.event("approval.requested")
        self.assertEqual(req("stop_task", command_id=self.cid(),
                             task_id="task-1")["status"], "accepted")
        feed.event("task.updated", status="stopped")

    def test_stop_ends_the_keeper_cleanly(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("approval.requested")
        self.assertEqual(feed.client.request(
            "stop", command_id="bye")["status"], "accepted")
        bye = feed.until(lambda f: f["t"] == "bye", "bye")
        self.assertEqual(bye["reason"], "stopped")
        feed.closed()
        # The agent was closed before the log: its last words are kept.
        types = [e["type"] for e in feed.events()]
        self.assertEqual(types[-3:], ["approval.resolved", "turn.ended",
                                      "session.state"])
        self.assertEqual(feed.events("session.state")[-1]["data"]["state"],
                         "closed")
        keeper._thread.join(WAIT)
        self.assertTrue(keeper.done)
        self.assertFalse(os.path.exists(keeper.socket_path))
        info = discovery.find(S, self.home)
        self.assertEqual((info.alive, info.state, info.lost),
                         (False, "exited", False))


class TestApprovals(KeeperCase):
    def test_hub_restart_reconnects_and_answers_a_pending_approval(self):
        keeper = self.keeper({"turns": [[fake.ask("rm -rf build?"),
                                         fake.say("done")]]})
        hub = self.connect(keeper)
        hub.live()
        self.send(hub, "clean up", command_id="c-1")
        hub.event("approval.requested")
        cursor = hub.seqs()[-1]
        hub.client.close()                      # the hub goes away

        self.assertTrue(keeper.sync(WAIT))      # the keeper does not
        found = discovery.discover(self.home, alive_only=True)
        self.assertEqual([i.session_key for i in found], [S])
        info = found[0]
        self.assertEqual((info.pid, info.ipc_version, info.state),
                         (os.getpid(), ipc.VERSION, "running"))
        client = discovery.connect(info, last_seq=cursor)
        self.addCleanup(client.close)
        new_hub = Feed(self, client)
        pending = client.welcome["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["data"]["prompt"], "rm -rf build?")
        self.assertEqual(client.welcome["open_turn"], "t1")
        new_hub.live()
        self.assertEqual(new_hub.seqs(), list(range(
            cursor + 1, client.welcome["last_seq"] + 1)))

        reply = client.request("respond", command_id="c-2",
                               request_id=pending[0]["data"]["request_id"],
                               decision="allow", device="phone")
        self.assertEqual(reply["status"], "accepted")
        resolved = new_hub.event("approval.resolved")
        self.assertEqual((resolved["data"]["decision"],
                          resolved["data"]["by"]), ("allow", "phone"))
        new_hub.event("turn.ended", status="completed")
        seqs = new_hub.seqs()
        self.assertEqual(seqs, list(range(cursor + 1, seqs[-1] + 1)))
        # The retried send from before the restart is still a duplicate.
        self.assertEqual(self.send(new_hub, "clean up",
                                   command_id="c-1")["status"], "duplicate")

    def test_two_clients_see_one_order_and_the_first_answer_wins(self):
        keeper = self.keeper({"turns": [[fake.ask("deploy?"),
                                         fake.say("ok")]]})
        phone = self.connect(keeper)
        mac = self.connect(keeper)
        phone.live()
        mac.live()
        self.assertEqual(self.status(phone)["clients"], 2)
        self.send(phone, "ship it", command_id="s1", device="phone")
        # The same send retried through the other client is one send.
        self.assertEqual(self.send(mac, "ship it",
                                   command_id="s1")["status"], "duplicate")
        phone.event("approval.requested")
        mac.event("approval.requested")
        first = mac.client.request("respond", command_id="m1",
                                   request_id="r1", decision="deny",
                                   device="mac")
        second = phone.client.request("respond", command_id="p1",
                                      request_id="r1", decision="allow",
                                      device="phone")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual((second["status"], second["reason"]),
                         ("rejected", "unknown_request"))
        for feed in (phone, mac):
            feed.event("turn.ended")
            feed.until(lambda f: f["t"] == "event"
                       and f["e"].get("command_id") == "p1", "p1's receipt")
        self.assertEqual(phone.events(), mac.events())
        resolved = phone.events("approval.resolved")
        self.assertEqual(len(resolved), 1)
        self.assertEqual((resolved[0]["data"]["decision"],
                          resolved[0]["data"]["by"]), ("deny", "mac"))
        responds = [c for c in self.adapters[0].calls if c.name == "respond"]
        self.assertEqual(len(responds), 1)

    def test_a_bad_answer_is_rejected(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("approval.requested")
        req = feed.client.request
        self.assertIn("decision", req("respond", command_id=self.cid(),
                                      request_id="r1",
                                      decision="maybe")["reason"])
        self.assertEqual(req("respond", command_id=self.cid(),
                             request_id="r1")["status"], "rejected")
        self.assertEqual(req("respond", command_id=self.cid(),
                             decision="allow")["reason"], "unknown_request")


class TestAgentCrash(KeeperCase):
    def test_crash_mid_turn_then_restart_on_the_next_send(self):
        crash = {"turns": [[fake.exit_agent("segfault")]]}
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]}, crash, crash,
                             crash, {"turns": [[fake.say("back")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "go", command_id="go")
        feed.event("approval.requested")
        self.adapters[0].crash("killed")
        down = feed.event("session.state", state="agent_down")
        self.assertEqual(down["data"]["reason"], "killed")
        tail = [e["type"] for e in feed.events()][-3:]
        self.assertEqual(tail, ["approval.resolved", "turn.ended",
                                "session.state"])
        self.assertEqual(feed.events("approval.resolved")[-1]["data"]
                         ["source"], "agent_exit")
        self.assertEqual(feed.events("turn.ended")[-1]["data"]["status"],
                         "failed")
        self.assertTrue(keeper.sync(WAIT))
        status = self.status(feed)
        self.assertEqual((status["agent"], status["state"],
                          status["pending"], status["restarts_left"]),
                         ("down", "agent_down", [], 3))
        self.assertEqual(discovery.find(S, self.home).state, "agent_down")
        # With no agent, only a send brings it back.
        self.assertEqual(feed.client.request(
            "interrupt", command_id=self.cid())["reason"], "agent_down")

        # Each of the next three sends restarts the agent, which dies
        # again mid-turn.
        for n in (1, 2, 3):
            self.assertEqual(self.send(feed, f"try {n}")["status"],
                             "accepted")
            feed.event("session.state", state="agent_down")
            self.assertTrue(keeper.sync(WAIT))
            self.assertEqual(self.adapters[n].spec.resume_id, "fake-session")
            self.assertEqual(self.status(feed)["restarts_left"], 3 - n)
        self.assertEqual(len(self.adapters), 4)

        refused = self.send(feed, "try 4")
        self.assertEqual(refused["status"], "rejected")
        self.assertIn("3 times in 10 minutes", refused["reason"])
        self.assertIn("try again in 10 min", refused["reason"])
        self.assertEqual(len(self.adapters), 4)

        self.clock.advance(600)
        self.assertEqual(self.send(feed, "try 5")["status"], "accepted")
        feed.event("turn.ended", status="completed")
        self.assertEqual(self.status(feed)["agent"], "up")
        self.assertEqual(len(self.adapters), 5)

    def test_queued_messages_wait_for_the_restart_and_go_first(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]},
                             {"turns": [[fake.say("b")], [fake.say("c")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        self.adapters[0].crash()
        feed.event("session.state", state="agent_down")
        self.assertTrue(keeper.sync(WAIT))
        self.assertEqual([i["command_id"]
                          for i in self.status(feed)["queue"]], ["b"])
        self.assertEqual(len(self.adapters), 1)   # no restart by itself
        self.assertEqual(self.send(feed, "c", command_id="c")["status"],
                         "queued")
        feed.result("c")
        self.assertEqual([e["command_id"]
                          for e in feed.events("turn.started")],
                         ["a", "b", "c"])

    def test_an_agent_that_will_not_start(self):
        class Broken(fake.FakeAdapter):
            def start(self, spec, sink):
                raise AdapterError("claude is not logged in",
                                   code="agent_down")

        made = []

        def factory():
            made.append(Broken() if len(made) < 2
                        else fake.FakeAdapter(clock=self.clock))
            return made[-1]

        keeper = Keeper(S, StartSpec(S, "/tmp", harness="claude"), factory,
                        home=self.home, runtime=self.runtime,
                        clock=self.clock,
                        log_options={"fsync": lambda fd: None,
                                     "background": False})
        keeper.start()
        self.addCleanup(self.stop, keeper)
        self.assertTrue(keeper.sync(WAIT))
        feed = self.connect(keeper)
        feed.live()
        error = feed.events("error")[0]
        self.assertEqual((error["data"]["code"], error["data"]["message"]),
                         ("agent_down", "claude is not logged in"))
        self.assertEqual(feed.events("session.state")[-1]["data"]["state"],
                         "agent_down")
        self.assertEqual(feed.client.welcome["agent"], "down")
        refused = self.send(feed, "hi")
        self.assertEqual((refused["status"], refused["reason"]),
                         ("rejected", "claude is not logged in"))
        self.assertEqual(self.send(feed, "hi again")["status"], "accepted")
        feed.event("turn.ended", status="completed")
        self.assertTrue(made[0].closed and made[1].closed)


class TestKeeperCrash(KeeperCase):
    def test_killed_keeper_is_tidied_by_the_next_one(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b", device="mac")
        feed.receipt("b")
        cursor = feed.seqs()[-1]
        kill(keeper)
        feed.closed()

        info = discovery.find(S, self.home)
        self.assertEqual((info.alive, info.lost, info.state),
                         (False, True, "running"))
        self.assertEqual(discovery.discover(self.home, alive_only=True), [])
        self.assertEqual(len(discovery.discover(self.home)), 1)

        second = self.keeper({"turns": [[fake.say("b done")]]})
        feed = self.connect(second, last_seq=cursor)
        self.assertTrue(feed.result("b")["data"]["ok"])
        events = feed.events()
        self.assertEqual(feed.seqs(),
                         list(range(cursor + 1, feed.seqs()[-1] + 1)))
        # What the dead keeper left open is closed first...
        self.assertEqual(
            [(e["type"], e["data"].get("source") or e["data"].get("status"))
             for e in events[:2]],
            [("approval.resolved", "agent_exit"),
             ("turn.ended", "keeper_lost")])
        self.assertEqual(events[1]["turn_id"], "t1")
        # ...the agent resumes the same harness session...
        self.assertEqual(self.adapters[1].spec.resume_id, "fake-session")
        # ...and the queued message, rebuilt from the log, is sent.
        started = feed.events("turn.started")
        self.assertEqual([e["command_id"] for e in started], ["b"])
        self.assertEqual(started[0]["data"]["by"], "mac")
        self.assertEqual(self.status(feed)["queue"], [])
        # Command ids survive too.
        for cid in ("a", "b"):
            self.assertEqual(self.send(feed, "again",
                                       command_id=cid)["status"],
                             "duplicate")

    def test_torn_log_line_is_cut_and_the_timeline_continues(self):
        keeper = self.keeper({"turns": [[fake.say("one")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("command.result", op="send")
        last = feed.seqs()[-1]
        segment = keeper.log.segments()[-1][1]
        kill(keeper)
        with open(segment, "ab") as fh:        # a crash mid-write
            fh.write(b'{"v":2,"type":"tool.updated","session":"cla')

        second = self.keeper({"turns": [[fake.say("two")]]})
        feed = self.connect(second)
        feed.live()
        status = self.status(feed)
        self.assertEqual(status["recovery"]["torn_bytes"], 43)
        self.send(feed, "more")
        feed.event("command.result", op="send")
        seqs = feed.seqs()
        self.assertEqual(seqs, list(range(1, seqs[-1] + 1)))
        self.assertGreater(seqs[-1], last + 3)
        for line in segment.read_bytes().splitlines():
            json.loads(line)                   # every line is whole

    def test_rotation_and_the_cap(self):
        turn = [fake.tool("cat", output="x" * 300) for _ in range(6)]
        keeper = self.keeper(
            {"turns": [list(turn) for _ in range(8)]},
            log_options={"segment_bytes": 2048, "cap_bytes": 8192})
        feed = self.connect(keeper)
        feed.live()
        for _ in range(8):
            self.send(feed)
            feed.event("command.result", op="send")
        last = feed.seqs()[-1]
        self.assertEqual(feed.seqs(), list(range(1, last + 1)))
        status = self.status(feed)
        self.assertGreater(status["log"]["segments"], 1)
        marker = status["marker"]
        self.assertEqual(marker["type"], "log.truncated")
        oldest = marker["before_seq"]
        self.assertEqual(status["oldest_seq"], oldest)
        self.assertGreater(oldest, 1)

        # A client that asks for dropped history is told so, then gets
        # everything that is left.
        old = self.connect(keeper, last_seq=0)
        self.assertEqual(old.client.welcome["oldest_seq"], oldest)
        old.live()
        gap = old.frames[0]
        self.assertEqual((gap["t"], gap["reason"], gap["oldest_seq"]),
                         ("gap", "evicted", oldest))
        self.assertEqual(gap["marker"], marker)
        self.assertEqual(old.seqs(), list(range(oldest, last + 1)))
        # One whose cursor is inside what is kept crosses segments
        # with no gap.
        recent = self.connect(keeper, last_seq=oldest + 2)
        recent.live()
        self.assertEqual(recent.frames[0]["t"], "event")
        self.assertEqual(recent.seqs(), list(range(oldest + 3, last + 1)))

    def test_a_clean_restart_keeps_the_queue_and_the_command_ids(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        feed.event("queue.updated")
        keeper.stop()
        self.assertTrue(keeper.done)

        second = self.keeper({"turns": [[fake.say("b done")]]})
        feed = self.connect(second)
        self.assertTrue(feed.result("b")["data"]["ok"])
        # A clean stop left nothing open, so nothing is retired twice.
        self.assertEqual(
            [e["data"]["status"] for e in feed.events("turn.ended")],
            ["interrupted", "completed"])
        self.assertEqual([e["command_id"]
                          for e in feed.events("turn.started")], ["a", "b"])
        self.assertEqual(self.send(feed, "x", command_id="a")["status"],
                         "duplicate")


class TestDurability(KeeperCase):
    """What a crash or a failing disk may and may not lose."""

    def flaky(self):
        """A log fsync that fails while ``broken`` is set."""
        broken = threading.Event()
        # The keeper says so in its output each time; not here.
        self.enterContext(mock.patch.object(keeper_process.logger,
                                            "warning"))

        def fsync(fd: int) -> None:
            if broken.is_set():
                raise OSError(28, "No space left on device")

        return broken, fsync

    def test_a_command_whose_receipt_is_not_on_disk_is_not_run(self):
        broken, fsync = self.flaky()
        keeper = self.keeper({"turns": [[fake.say("one")]]},
                             log_options={"fsync": fsync})
        feed = self.connect(keeper)
        feed.live()
        broken.set()
        refused = self.send(feed, "hello", command_id="c-1")
        self.assertEqual((refused["status"], refused["reason"]),
                         ("rejected", "not_recorded"))
        self.assertTrue(keeper.sync(WAIT))
        self.assertEqual([c.name for c in self.adapters[0].calls],
                         ["start"])
        broken.clear()
        # Under that id the command stays refused, whatever a later
        # keeper finds in the log: it is never applied late.
        again = self.send(feed, "hello", command_id="c-1")
        self.assertEqual((again["status"], again["first"]),
                         ("duplicate", "rejected"))
        self.assertEqual(self.send(feed, "hello",
                                   command_id="c-2")["status"], "accepted")
        feed.event("turn.ended", status="completed")
        self.assertEqual(
            [e["command_id"] for e in feed.events("turn.started")], ["c-2"])

    def test_a_write_that_fails_leaves_the_id_free(self):
        keeper = self.keeper({"turns": [[fake.say("one")]]})
        feed = self.connect(keeper)
        feed.live()
        with mock.patch.object(keeper.log, "emit", return_value=None):
            refused = self.send(feed, "hello", command_id="c-1")
            self.assertEqual((refused["status"], refused["reason"]),
                             ("rejected", "not_recorded"))
        self.assertEqual(self.send(feed, "hello",
                                   command_id="c-1")["status"], "accepted")
        feed.event("turn.ended", status="completed")

    def test_the_queue_is_written_before_its_receipt(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        feed.receipt("b")
        types = [(e["type"], e.get("command_id")) for e in feed.events()]
        self.assertLess(types.index(("queue.updated", None)),
                        types.index(("command.receipt", "b")))

    def test_a_queued_message_without_a_receipt_is_still_taken(self):
        # A keeper that died between writing the queue and the receipt.
        log = EventLog(S, home=self.home, fsync=lambda fd: None,
                       background=False)
        log.emit(ev.queue_updated(S, [{"command_id": "b", "text": "b",
                                       "mode": "queue"}]))
        log.close()
        keeper = self.keeper({"turns": [[fake.say("b done")]]})
        feed = self.connect(keeper)
        self.assertTrue(feed.result("b")["data"]["ok"])
        self.assertEqual(self.send(feed, "b", command_id="b")["status"],
                         "duplicate")
        self.assertEqual(len(feed.events("turn.started")), 1)

    def test_a_queued_send_that_cannot_be_recorded_is_taken_back(self):
        broken, fsync = self.flaky()
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]},
                             log_options={"fsync": fsync})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        broken.set()
        req = feed.client.request
        self.assertEqual(self.send(feed, "c", command_id="c")["reason"],
                         "not_recorded")
        self.assertEqual(req("queue_edit", command_id="e1", target="b",
                             text="changed")["reason"], "not_recorded")
        self.assertEqual(req("queue_remove", command_id="e2",
                             target="b")["reason"], "not_recorded")
        broken.clear()
        self.assertEqual(self.status(feed)["queue"], [
            {"command_id": "b", "text": "b", "mode": "queue"}])

    def test_replay_reports_history_dropped_on_any_page(self):
        # The cap can drop segments a replay has not reached yet: the
        # second page then comes back truncated.
        e = [dict(ev.tool_updated(S, f"c{i}", text="x"), seq=i)
             for i in range(1, 9)]
        pages = [Replay(e[0:2], oldest_seq=1, more=True),
                 Replay(e[5:7], oldest_seq=6, truncated=True,
                        reason="evicted", more=True,
                        marker={"type": "log.truncated", "before_seq": 6}),
                 Replay(e[7:8], oldest_seq=6)]
        log = mock.Mock()
        log.replay.side_effect = pages
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        conn = keeper_process._Conn(mock.Mock(log=log, replay_page=2), a, 1)
        conn._replay(0, 8)
        reader = ipc.LineReader(b)
        frames = [reader.read(WAIT) for _ in range(7)]
        self.assertEqual(
            [f["t"] if f["t"] != "event" else f["e"]["seq"] for f in frames],
            [1, 2, "gap", 6, 7, 8, "live"])
        self.assertEqual((frames[2]["oldest_seq"], frames[2]["reason"]),
                         (6, "evicted"))
        self.assertEqual([c.args[0] for c in log.replay.call_args_list],
                         [0, 2, 7])


class TestLimits(KeeperCase):
    def test_a_message_too_large_for_a_frame_is_refused(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        big = "x" * (keeper_process.MAX_TEXT_BYTES + 1)
        reply = self.send(feed, big)
        self.assertEqual((reply["status"], reply["reason"]),
                         ("rejected", "too_large"))
        reply = self.send(feed, "hi", attachments=["/a"] * 101)
        self.assertEqual(reply["reason"], "too_large")
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, "b", command_id="b")
        self.assertEqual(feed.client.request(
            "queue_edit", command_id=self.cid(), target="b",
            text=big)["reason"], "too_large")

    def test_the_queue_is_bounded_so_it_always_fits_one_frame(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a")
        feed.event("approval.requested")
        with mock.patch.object(keeper_process, "QUEUE_MAX_ITEMS", 2):
            self.assertEqual(self.send(feed, "b")["status"], "queued")
            self.assertEqual(self.send(feed, "c")["status"], "queued")
            full = self.send(feed, "d")
            self.assertEqual((full["status"], full["reason"]),
                             ("rejected", "queue_full"))
        with mock.patch.object(keeper_process, "QUEUE_MAX_BYTES", 400):
            self.assertEqual(self.send(feed, "y" * 300)["reason"],
                             "queue_full")
            self.assertEqual(feed.client.request(
                "queue_edit", command_id=self.cid(), target="c3",
                text="y" * 300)["reason"], "too_large")
        self.assertEqual(len(self.status(feed)["queue"]), 2)
        self.assertLess(keeper_process.QUEUE_MAX_BYTES * 2, ipc.MAX_LINE)

    def test_a_queued_secret_is_redacted_where_it_is_shown(self):
        secret = "use OPENAI_API_KEY=sk-test for it"
        keeper = self.keeper({"turns": [[fake.ask("ok?")],
                                        [fake.say("done")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "a", command_id="a")
        feed.event("approval.requested")
        self.send(feed, secret, command_id="b")
        shown = feed.event("queue.updated")["data"]["items"][0]["text"]
        self.assertEqual(shown, "use OPENAI_API_KEY=[REDACTED] for it")
        self.assertEqual(self.status(feed)["queue"][0]["text"], shown)
        self.assertEqual(self.connect(keeper, last_seq=None)
                         .client.welcome["queue"][0]["text"], shown)
        segment = keeper.log.segments()[-1][1]
        self.assertNotIn(b"sk-test", segment.read_bytes())
        # The agent still gets the message as it was typed.
        feed.client.request("respond", command_id="ok", request_id="r1",
                            decision="allow")
        feed.result("b")
        sent = [c.args[0].text for c in self.adapters[0].calls
                if c.name == "send_turn"]
        self.assertEqual(sent, ["a", secret])


class TestOptionsPersist(KeeperCase):
    CAPS = Caps(plan_mode=True,
                options=frozenset({"model", "permission_mode"}))

    def test_an_option_change_is_kept_for_the_next_keeper(self):
        discovery.write_spec(S, harness="claude", cwd="/tmp", model="opus",
                             home=self.home)
        keeper = self.keeper({"caps": self.CAPS})
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        feed.result("m")
        spec = discovery.read_spec(S, self.home)
        self.assertEqual(spec["model"], "haiku")
        # A change the agent refused is not kept.
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.assertEqual(discovery.read_spec(S, self.home)["model"],
                         "haiku")
        self.assertFalse(discovery.update_spec("claude:none", self.home,
                                               model="x"))

    def test_choosing_a_permission_mode_leaves_plan_mode(self):
        discovery.write_spec(S, harness="claude", cwd="/tmp",
                             plan_mode=True, home=self.home)
        spec = StartSpec(S, "/tmp", harness="claude", plan_mode=True)
        keeper = self.keeper({"caps": self.CAPS}, {"caps": self.CAPS},
                             spec=spec)
        feed = self.connect(keeper)
        feed.live()
        self.assertTrue(self.adapters[0].spec.plan_mode)
        feed.client.request("set_option", command_id="m",
                            name="permission_mode", value="default")
        feed.result("m")
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed)
        feed.event("turn.ended")
        resumed = self.adapters[1].spec
        self.assertEqual((resumed.plan_mode, resumed.permission_mode),
                         (False, "default"))
        stored = discovery.read_spec(S, self.home)
        self.assertEqual((stored["plan_mode"], stored["permission_mode"]),
                         (False, "default"))

    def test_an_approved_plan_ends_plan_mode(self):
        spec = StartSpec(S, "/tmp", harness="claude", plan_mode=True)
        keeper = self.keeper(
            {"caps": self.CAPS,
             "turns": [[fake.ask("the plan", kind="plan")]]},
            {"caps": self.CAPS}, spec=spec)
        feed = self.connect(keeper)
        feed.live()
        self.send(feed, "plan it")
        feed.event("approval.requested")
        feed.client.request("respond", command_id="ok", request_id="r1",
                            decision="allow")
        feed.event("turn.ended")
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed, "go")
        feed.event("turn.ended")
        self.assertFalse(self.adapters[1].spec.plan_mode)

    def test_turning_plan_mode_on_keeps_the_permission_mode(self):
        keeper = self.keeper({"caps": self.CAPS}, {"caps": self.CAPS})
        feed = self.connect(keeper)
        feed.live()
        for cid, name, value in (("a", "permission_mode", "acceptEdits"),
                                 ("b", "plan_mode", True)):
            feed.client.request("set_option", command_id=cid, name=name,
                                value=value)
            feed.result(cid)
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed)
        feed.event("turn.ended")
        resumed = self.adapters[1].spec
        self.assertEqual((resumed.plan_mode, resumed.permission_mode),
                         (True, "acceptEdits"))


class TestIdlePark(KeeperCase):
    def test_park_after_thirty_idle_minutes_and_resume_on_send(self):
        position = fake.TranscriptPosition("/h/.claude/t.jsonl", 4096)
        keeper = self.keeper({"turns": [[fake.say("one")]],
                              "position": position},
                             {"turns": [[fake.say("two")]]})
        self.assertEqual(keeper.park_after, 30 * 60)
        feed = self.connect(keeper)
        feed.live()
        # A turn in between restarts the count.
        self.clock.advance(20 * 60)
        self.send(feed, "one")
        feed.event("turn.ended")
        self.assertTrue(keeper.sync(WAIT))
        self.clock.advance(29 * 60 + 59)
        keeper.poke()
        self.assertTrue(keeper.sync(WAIT))
        self.assertEqual(self.status(feed)["agent"], "up")

        self.clock.advance(1)
        keeper.poke()
        parked = feed.event("session.state", state="parked")
        self.assertEqual(parked["data"]["reason"], "idle for 30 minutes")
        # The agent is closed; the session is not.
        self.assertNotIn("closed", [e["data"]["state"]
                                    for e in feed.events("session.state")])
        self.assertTrue(self.adapters[0].closed)
        self.assertTrue(keeper.sync(WAIT))
        status = self.status(feed)
        self.assertEqual((status["agent"], status["state"]),
                         ("parked", "parked"))
        self.assertEqual(status["transcript"],
                         {"path": "/h/.claude/t.jsonl", "offset": 4096})
        info = discovery.find(S, self.home)
        self.assertEqual((info.alive, info.state), (True, "parked"))
        self.assertEqual(info.raw["transcript"]["offset"], 4096)
        self.assertEqual(feed.client.request(
            "interrupt", command_id=self.cid())["reason"], "parked")

        self.assertEqual(self.send(feed, "two")["status"], "accepted")
        feed.event("turn.ended", status="completed")
        self.assertEqual(self.adapters[1].spec.resume_id, "fake-session")
        status = self.status(feed)
        # Waking a parked session is not a crash restart.
        self.assertEqual((status["agent"], status["restarts_left"]),
                         ("up", 3))

    def test_a_waiting_or_busy_session_is_never_parked(self):
        keeper = self.keeper({"turns": [[fake.ask("ok?")]]})
        feed = self.connect(keeper)
        feed.live()
        self.send(feed)
        feed.event("approval.requested")
        self.clock.advance(4 * 60 * 60)
        keeper.poke()
        self.assertTrue(keeper.sync(WAIT))
        keeper.poke()
        self.assertTrue(keeper.sync(WAIT))
        self.assertEqual(self.status(feed)["agent"], "up")
        reply = feed.client.request("park", command_id=self.cid())
        self.assertEqual((reply["status"], reply["reason"]),
                         ("rejected", "busy"))


class TestSlowClient(KeeperCase):
    def raw(self, keeper: Keeper, name: str) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.connect(str(keeper.socket_path))
        ipc.send_frame(sock, {"t": "hello", "v": 1, "last_seq": None,
                              "client": name})
        self.assertEqual(ipc.LineReader(sock).read(WAIT)["t"], "welcome")
        return sock

    def conn(self, keeper: Keeper, name: str):
        self.assertTrue(keeper.sync(WAIT))
        with keeper._conns_lock:
            return [c for c in keeper._conns if c.name == name][0]

    def test_a_client_that_falls_behind_is_told_and_dropped(self):
        big = "x" * 100_000
        keeper = self.keeper(
            {"autoplay": False,
             "turns": [[fake.tool("cat", output=big) for _ in range(40)]]},
            client_buffer=10)
        feed = self.connect(keeper)
        feed.live()
        slow = self.raw(keeper, "slow")
        reader = ipc.LineReader(slow)
        self.send(feed)
        feed.event("message.completed", kind="user")
        adapter = self.adapters[0]
        # One tool call at a time, so the client that reads keeps up.
        for _ in range(40):
            adapter.step()
            feed.event("tool.completed")
        adapter.step()
        feed.event("turn.ended", status="completed")
        # The slow one starts reading too late: it gets what was queued
        # before it overflowed, then a goodbye.
        frames = []
        while True:
            frame = reader.read(WAIT)
            if frame is None:
                break
            frames.append(frame)
        self.assertEqual(frames[-1], {"t": "bye", "reason": "overflow"})
        got = [f for f in frames if f["t"] == "event"]
        self.assertLess(len(got), 40)
        self.assertEqual(self.status(feed)["clients"], 1)
        # Its cursor is good for a replay of the rest.
        cursor = max(f["e"]["seq"] for f in got)
        again = self.connect(keeper, last_seq=cursor)
        last = again.live()["last_seq"]
        self.assertEqual(again.seqs(), list(range(cursor + 1, last + 1)))

    def test_a_client_that_stops_reading_never_stalls_the_keeper(self):
        big = "x" * 100_000
        keeper = self.keeper(
            {"turns": [[fake.tool("cat", output=big) for _ in range(60)]]},
            send_timeout=0.2)
        feed = self.connect(keeper)
        feed.live()
        self.raw(keeper, "deaf")
        deaf = self.conn(keeper, "deaf")
        self.send(feed)
        feed.event("turn.ended", status="completed")
        self.assertEqual(len(feed.events("tool.completed")), 60)
        deaf.writer.join(WAIT)
        self.assertFalse(deaf.writer.is_alive())
        self.assertEqual(self.status(feed)["clients"], 1)


class TestSocketSafety(KeeperCase):
    def test_the_socket_and_its_folder_are_private(self):
        keeper = self.keeper()
        self.assertEqual(keeper.socket_path.parent, self.runtime)
        self.assertEqual(stat.S_IMODE(os.stat(self.runtime).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(keeper.socket_path).st_mode),
                         0o600)
        folder = discovery.keeper_dir_for(S, self.home)
        self.assertEqual(folder.name, key_slug(S))
        for name in (discovery.KEEPER_FILE, discovery.LOCK_FILE):
            self.assertEqual(stat.S_IMODE(os.stat(folder / name).st_mode),
                             0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    def test_a_folder_others_can_enter_is_refused_by_both_ends(self):
        keeper = self.keeper()
        os.chmod(self.runtime, 0o755)
        client = ipc.KeeperClient(keeper.socket_path)
        with self.assertRaises(ipc.Unsafe) as ctx:
            client.connect()
        self.assertIn("open to other users", str(ctx.exception))
        other = self.keeper(start=False, session="claude:other")
        with self.assertRaises(ipc.Unsafe):
            other.open()
        # A refused open holds nothing: the same keeper opens once the
        # folder is private again.
        os.chmod(self.runtime, 0o700)
        other.start()
        self.addCleanup(self.stop, other)
        self.connect(other).live()

    def test_a_link_or_a_file_is_not_a_socket_folder(self):
        target = self.home / "elsewhere"
        target.mkdir(mode=0o700)
        link = self.home / "link"
        link.symlink_to(target)
        with self.assertRaises(ipc.Unsafe):
            ipc.ensure_runtime_dir(link)
        plain = self.home / "plain"
        plain.write_text("x")
        with self.assertRaises(ipc.Unsafe) as ctx:
            ipc.ensure_runtime_dir(plain)
        self.assertIn("not a folder", str(ctx.exception))
        with self.assertRaises(ipc.Unsafe):
            ipc.check_private_dir(self.home / "missing")

    def test_only_a_socket_of_this_user_is_connected_to(self):
        self.runtime.mkdir(mode=0o700)
        with self.assertRaises(ipc.IpcError):
            ipc.KeeperClient(self.runtime / "k-none.sock").connect()
        plain = self.runtime / "k-file.sock"
        plain.write_text("x")
        with self.assertRaises(ipc.Unsafe):
            ipc.KeeperClient(plain).connect()

    def test_socket_names(self):
        a = ipc.socket_path("/h1/keepers/abc", "/tmp/swe-1")
        b = ipc.socket_path("/h2/keepers/abc", "/tmp/swe-1")
        self.assertNotEqual(a, b)              # one name per home
        self.assertRegex(a.name, r"^k-[0-9a-f]{24}\.sock$")
        with self.assertRaises(ipc.Unsafe):
            ipc.socket_path("/h1/keepers/abc", "/tmp/" + "d" * 90)
        self.assertEqual(ipc.runtime_dir(), Path(os.environ.get(
            ipc.RUNTIME_ENV) or f"/tmp/swe-{os.getuid()}"))

    def test_a_frame_with_a_lone_surrogate_survives_the_wire(self):
        frame = {"t": "event", "e": {"text": "café \ud83d"}}
        raw = ipc.encode(frame)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), frame)
        self.assertIn("café".encode(), ipc.encode({"x": "café"}))

    def test_a_frame_past_the_size_limit_ends_the_read(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        reader = ipc.LineReader(b, max_line=64)
        a.sendall(b"x" * 200)
        with self.assertRaises(ipc.IpcError):
            reader.read(WAIT)
        a.sendall(b"\n[1]\n")
        with self.assertRaises(ipc.IpcError):
            ipc.LineReader(b).read(WAIT)
        with self.assertRaises(TimeoutError):
            ipc.LineReader(b).read(0.01)
        # A line past the limit that arrives whole, newline and all, in
        # one read is refused just the same.
        c, d = socket.socketpair()
        self.addCleanup(c.close)
        self.addCleanup(d.close)
        c.sendall(b'{"pad":"' + b"x" * 100 + b'"}\n{"ok":1}\n')
        reader = ipc.LineReader(d, max_line=64)
        with self.assertRaises(ipc.IpcError):
            reader.read(WAIT)
        c.sendall(b'{"pad":"' + b"x" * 40 + b'"}\n')
        self.assertEqual(ipc.LineReader(d, max_line=64).read(WAIT),
                         {"pad": "x" * 40})

    def test_a_request_to_a_keeper_that_stopped_reading_times_out(self):
        self.runtime.mkdir(mode=0o700)
        path = self.runtime / "k-stuck.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        client = ipc.KeeperClient(path, send_timeout=0.2)
        self.addCleanup(client.close)

        def welcome() -> socket.socket:
            conn, _ = server.accept()
            ipc.LineReader(conn).read(WAIT)
            ipc.send_frame(conn, {"t": "welcome", "v": 1, "last_seq": 0})
            return conn          # and never reads again

        held: list[socket.socket] = []
        thread = threading.Thread(target=lambda: held.append(welcome()))
        thread.start()
        client.connect()
        thread.join(WAIT)
        self.addCleanup(held[0].close)
        # The write itself blocks once the socket's buffer is full; it
        # is bounded, and the stream is then given up.
        with self.assertRaises(ipc.IpcError) as ctx:
            client.request("send", timeout=WAIT, command_id="x",
                           text="y" * 4_000_000)
        self.assertIn("could not reach the keeper", str(ctx.exception))
        with self.assertRaises(ipc.IpcError):
            client.request("status")


class TestDiscovery(KeeperCase):
    def test_one_keeper_per_session(self):
        keeper = self.keeper()
        twin = self.keeper(start=False)
        with self.assertRaises(discovery.KeeperRunning):
            twin.open(lock_tries=2, sleep=lambda s: None)
        self.assertEqual(discovery.find(S, self.home).pid, os.getpid())
        self.connect(keeper).live()

    def test_discover_lists_every_keeper_folder(self):
        self.assertEqual(discovery.discover(self.home), [])
        self.assertIsNone(discovery.find(S, self.home))
        one = self.keeper(session="claude:one")
        two = self.keeper(session="claude:two")
        # A folder with a log and no keeper.json is not a keeper.
        (discovery.keepers_root(self.home) / "stray" / "log").mkdir(
            parents=True)
        (discovery.keepers_root(self.home) / "note.txt").write_text("x")
        found = discovery.discover(self.home)
        self.assertEqual([i.session_key for i in found],
                         ["claude:one", "claude:two"])
        self.assertTrue(all(i.alive and not i.lost for i in found))
        self.assertEqual(found[0].socket, str(one.socket_path))
        self.assertEqual(found[0].slug, key_slug("claude:one"))
        self.assertEqual(found[0].harness_session_id, "fake-session")
        two.stop()
        kill(one)
        by_key = {i.session_key: i for i in discovery.discover(self.home)}
        self.assertEqual((by_key["claude:one"].alive,
                          by_key["claude:one"].lost), (False, True))
        self.assertEqual((by_key["claude:two"].alive,
                          by_key["claude:two"].lost,
                          by_key["claude:two"].state),
                         (False, False, "exited"))
        self.assertEqual(discovery.discover(self.home, alive_only=True), [])

    def test_a_damaged_keeper_json_is_read_as_far_as_it_goes(self):
        folder = discovery.keeper_dir_for(S, self.home)
        folder.mkdir(parents=True)
        (folder / discovery.KEEPER_FILE).write_text("[1, 2]")
        self.assertIsNone(discovery.find(S, self.home))
        (folder / discovery.KEEPER_FILE).write_text(json.dumps(
            {"session": 7, "pid": "x", "ipc": "v1", "started_at": True}))
        info = discovery.find(S, self.home)
        self.assertEqual((info.session_key, info.pid, info.ipc_version,
                          info.started_at, info.alive),
                         ("", 0, 0, 0.0, False))

    def test_spec_round_trip(self):
        path = discovery.write_spec(
            S, harness="claude", cwd="/work", resume_id="abc", model="opus",
            plan_mode=True, env={"A": "1"}, first_seq=42, home=self.home)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(discovery.read_spec(S, self.home), {
            "session": S, "harness": "claude", "cwd": "/work",
            "resume_id": "abc", "model": "opus", "effort": "",
            "permission_mode": "", "fork": False, "plan_mode": True,
            "env": {"A": "1"}, "first_seq": 42})
        self.assertIsNone(discovery.read_spec("claude:other", self.home))
        with self.assertRaises(ValueError):
            discovery.write_spec(S, harness="claude", cwd="relative",
                                 home=self.home)
        with self.assertRaises(ValueError):
            discovery.write_spec(S, harness="claude", cwd="/w", first_seq=0,
                                 home=self.home)
        path.write_text(json.dumps({"session": S, "harness": "",
                                    "cwd": "/w"}))
        self.assertIsNone(discovery.read_spec(S, self.home))

    def test_first_seq_continues_a_hub_journal(self):
        keeper = self.keeper(first_seq=500)
        feed = self.connect(keeper, last_seq=499)
        feed.live()
        self.assertEqual(feed.client.welcome["first_seq"], 500)
        self.assertEqual(feed.seqs()[0], 500)
        # History before the log began is the hub journal's.
        old = self.connect(keeper, last_seq=10)
        old.live()
        self.assertEqual((old.frames[0]["t"], old.frames[0]["reason"]),
                         ("gap", "before_start"))

    def test_keeper_argv(self):
        self.assertEqual(discovery.keeper_argv("claude:x", "/py"),
                         ["/py", "-m", "swe.remote.keeper",
                          "--session=claude:x"])
        self.assertEqual(discovery.keeper_argv("k")[0], sys.executable)

    def test_the_wrong_spec_is_refused(self):
        with self.assertRaises(ValueError):
            Keeper(S, StartSpec("claude:zzz", "/tmp"), self.factory,
                   home=self.home)
        with self.assertRaises(RuntimeError):
            self.keeper(start=False).run()


class TestServiceManagers(unittest.TestCase):
    def test_the_launch_agent_leaves_the_jobs_children_running(self):
        plist = launchd.build_plist(["swe", "remote", "serve"],
                                    Path("/tmp/serve.log"), "/usr/bin")
        self.assertIs(plist["AbandonProcessGroup"], True)

    def test_keeps_children_reads_the_installed_plist(self):
        import plistlib

        home = short_home(self)
        self.assertIsNone(launchd.keeps_children(home))
        path = launchd.plist_path(home)
        path.parent.mkdir(parents=True)
        old = launchd.build_plist(["swe"], Path("/tmp/x.log"), "/usr/bin")
        del old["AbandonProcessGroup"]
        path.write_bytes(plistlib.dumps(old))
        self.assertIs(launchd.keeps_children(home), False)
        path.write_bytes(plistlib.dumps(launchd.build_plist(
            ["swe"], Path("/tmp/x.log"), "/usr/bin")))
        self.assertIs(launchd.keeps_children(home), True)
        path.write_bytes(b"not a plist")
        self.assertIsNone(launchd.keeps_children(home))

    def test_a_keeper_gets_its_own_systemd_scope(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        argv = systemd.keeper_scope_argv(
            "abc123", run=run, which=lambda name: f"/usr/bin/{name}",
            platform="linux")
        self.assertEqual(argv, [
            "systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--unit=swe-keeper-abc123", "--"])
        self.assertEqual(calls, [["systemctl", "--user",
                                  "show-environment"]])
        self.assertEqual(systemd.keeper_unit("abc123"), "swe-keeper-abc123")

    def test_no_scope_without_a_user_manager(self):
        def down(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "no bus")

        def ok(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "", "")

        present = lambda name: f"/usr/bin/{name}"  # noqa: E731
        self.assertIsNone(systemd.keeper_scope_argv(
            "s", run=down, which=present, platform="linux"))
        self.assertIsNone(systemd.keeper_scope_argv(
            "s", run=ok, which=lambda name: None, platform="linux"))
        self.assertIsNone(systemd.keeper_scope_argv(
            "s", run=ok, which=present, platform="darwin"))


class RealProcess(unittest.TestCase):
    """``python -m swe.remote.keeper`` as the hub starts it, driving the
    fake ``claude``."""

    def setUp(self) -> None:
        self.home = short_home(self)
        self.runtime = self.home / "rt"
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self.reap)
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "harness.json").write_text(json.dumps({"claude": {
            "capabilities": {"session": {
                "adapter": "claude_stream",
                "argv": [sys.executable, str(FAKE_CLAUDE)]}}}}))
        self.agent_log = self.home / "agent.log"

    def popen(self, *args, **kw) -> subprocess.Popen:
        proc = subprocess.Popen(*args, **kw)
        self.procs.append(proc)
        return proc

    def reap(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(WAIT)

    def spawn(self, session: str = S, mode: str = "approval",
              **kw) -> discovery.KeeperInfo:
        kw.setdefault("scope", lambda slug: None)
        return discovery.spawn(
            session, home=self.home, runtime=self.runtime, popen=self.popen,
            env={"FAKE_CLAUDE_MODE": mode,
                 "FAKE_CLAUDE_LOG": str(self.agent_log),
                 "PYTHONPATH": str(SRC)}, **kw)

    def feed(self, info: discovery.KeeperInfo,
             last_seq: int | None = 0) -> Feed:
        client = discovery.connect(info, last_seq=last_seq)
        self.addCleanup(client.close)
        return Feed(self, client)

    def agent_argvs(self) -> list[list[str]]:
        lines = [json.loads(line) for line in
                 self.agent_log.read_text().splitlines()]
        return [line["argv"] for line in lines if "argv" in line]

    def test_kill_the_keeper_then_start_another(self):
        discovery.write_spec(S, harness="claude", cwd=str(self.home),
                             home=self.home)
        info = self.spawn()
        self.assertTrue(info.alive)
        self.assertEqual(info.pid, self.procs[0].pid)
        self.assertNotEqual(info.pid, os.getpid())
        with self.assertRaises(discovery.KeeperRunning):
            self.spawn()

        feed = self.feed(info)
        idle = feed.event("session.state", state="idle")
        session_id = idle["data"]["harness_session_id"]
        self.assertEqual(feed.client.request(
            "send", command_id="a", text="first")["status"], "accepted")
        asked = feed.event("approval.requested")
        self.assertEqual(feed.client.request(
            "send", command_id="b", text="second")["status"], "queued")
        feed.receipt("b")
        cursor = feed.seqs()[-1]

        os.kill(info.pid, signal.SIGKILL)
        self.procs[0].wait(WAIT)
        feed.closed()
        lost = discovery.find(S, self.home)
        self.assertEqual((lost.alive, lost.lost), (False, True))

        again = self.spawn()
        self.assertNotEqual(again.pid, info.pid)
        feed = self.feed(again, last_seq=cursor)
        retired = feed.event("approval.resolved")
        self.assertEqual(
            (retired["data"]["request_id"], retired["data"]["source"]),
            (asked["data"]["request_id"], "agent_exit"))
        ended = feed.event("turn.ended")
        self.assertEqual((ended["data"]["status"], ended["turn_id"]),
                         ("keeper_lost", asked["turn_id"]))
        # The new agent resumed the same session, and the queued
        # message was sent to it.
        second = feed.event("approval.requested")
        self.assertIn(f"--resume={session_id}",
                      " ".join(self.agent_argvs()[-1]).replace(
                          "--resume ", "--resume="))
        started = feed.events("turn.started")
        self.assertEqual([e["command_id"] for e in started], ["b"])
        self.assertEqual(feed.client.request(
            "respond", command_id="ok",
            request_id=second["data"]["request_id"],
            decision="allow")["status"], "accepted")
        feed.event("turn.ended", status="completed")
        seqs = feed.seqs()
        self.assertEqual(seqs, list(range(cursor + 1, seqs[-1] + 1)))
        self.assertEqual(feed.client.request(
            "send", command_id="a", text="first")["status"], "duplicate")

        self.assertEqual(feed.client.request(
            "stop", command_id="stop")["status"], "accepted")
        feed.closed()
        self.assertEqual(self.procs[-1].wait(WAIT), discovery.EXIT_OK)
        done = discovery.find(S, self.home)
        self.assertEqual((done.alive, done.state), (False, "exited"))

    def test_an_agent_that_outlives_its_keeper_is_ended_by_the_next(self):
        # ``linger``: the agent ignores the end of its stdin, so it does
        # not exit when its keeper is killed.
        discovery.write_spec(S, harness="claude", cwd=str(self.home),
                             home=self.home)
        info = self.spawn(mode="linger")
        feed = self.feed(info)
        feed.event("session.state", state="idle")
        children = subprocess.run(
            ["pgrep", "-P", str(info.pid)], capture_output=True,
            text=True).stdout.split()
        self.assertEqual(len(children), 1)
        agent = int(children[0])
        os.kill(info.pid, signal.SIGKILL)
        self.procs[0].wait(WAIT)
        feed.closed()
        os.kill(agent, 0)                       # still running

        again = self.spawn(mode="ok")
        # The next keeper ended it before starting an agent of its own.
        with self.assertRaises(ProcessLookupError):
            os.kill(agent, 0)
        feed = self.feed(again)
        feed.event("session.state", state="idle")
        self.assertIn("a dead keeper",
                      (again.dir / discovery.OUT_FILE).read_text())
        feed.client.request("stop", command_id="stop")
        self.assertEqual(self.procs[-1].wait(WAIT), discovery.EXIT_OK)

    def test_sigterm_stops_it_cleanly(self):
        discovery.write_spec(S, harness="claude", cwd=str(self.home),
                             home=self.home)
        info = self.spawn(mode="ok")
        feed = self.feed(info)
        feed.event("session.state", state="idle")
        os.kill(info.pid, signal.SIGTERM)
        bye = feed.until(lambda f: f["t"] == "bye", "bye")
        self.assertEqual(bye["reason"], "signal")
        self.assertEqual(self.procs[0].wait(WAIT), discovery.EXIT_OK)
        self.assertEqual(discovery.find(S, self.home).state, "exited")

    def test_a_failed_scope_wrapper_falls_back_to_a_plain_child(self):
        discovery.write_spec(S, harness="claude", cwd=str(self.home),
                             home=self.home)
        wrapper = [sys.executable, "-c", "import sys; sys.exit(1)"]
        info = self.spawn(mode="ok", scope=lambda slug: wrapper)
        self.assertEqual(len(self.procs), 2)
        self.assertEqual(self.procs[0].returncode, 1)
        self.assertEqual(info.pid, self.procs[1].pid)
        feed = self.feed(info)
        feed.event("session.state", state="idle")
        feed.client.request("stop", command_id="stop")
        self.assertEqual(self.procs[1].wait(WAIT), discovery.EXIT_OK)

    def test_spawn_reports_a_keeper_that_cannot_start(self):
        with self.assertRaises(discovery.SpawnError) as ctx:
            self.spawn("claude:nospec")
        self.assertIn("write_spec first", str(ctx.exception))
        discovery.write_spec("nope:1", harness="nope", cwd="/tmp",
                             home=self.home)
        with self.assertRaises(discovery.SpawnError) as ctx:
            self.spawn("nope:1")
        self.assertIn(f"status {discovery.EXIT_NO_ADAPTER}",
                      str(ctx.exception))
        self.assertIn("does not know how to drive nope",
                      ctx.exception.output)
        self.assertEqual(len(self.procs), 1)   # no second try

    def test_spawn_gives_up_on_a_keeper_that_never_answers(self):
        discovery.write_spec(S, harness="claude", cwd=str(self.home),
                             home=self.home)
        now = [0.0]

        def clock() -> float:
            now[0] += 1.0
            return now[0]

        def sleeper(*args, **kw) -> subprocess.Popen:
            return self.popen([sys.executable, "-c",
                               "import time; time.sleep(600)"])

        with self.assertRaises(discovery.SpawnError) as ctx:
            discovery.spawn(S, home=self.home, runtime=self.runtime,
                            popen=sleeper, scope=lambda slug: None,
                            wait_s=3.0, sleep=lambda s: None, clock=clock)
        self.assertIn("did not come up in 3 s", str(ctx.exception))
        self.assertIsNotNone(self.procs[0].returncode)

    def test_a_keeper_does_not_carry_the_hub(self):
        # One process per live session: importing the hub with every
        # keeper cost about 23 MB each.
        done = subprocess.run(
            [sys.executable, "-c",
             "import sys, swe.remote.keeper.__main__; "
             "print(sorted(m for m in sys.modules if m.startswith('swe.')))"],
            env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True,
            text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        loaded = done.stdout
        self.assertIn("swe.remote.keeper.process", loaded)
        self.assertIn("swe.remote.adapters.claude_stream", loaded)
        for heavy in ("swe.remote.hub", "swe.remote.commands",
                      "swe.remote.tail", "swe.webapp", "swe.ledger"):
            self.assertNotIn(f"'{heavy}", loaded)
        from swe import remote
        from swe.remote import commands

        self.assertIs(remote.cmd_remote, commands.cmd_remote)
        with self.assertRaises(AttributeError):
            remote.no_such_name

    def test_usage_errors(self):
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(SRC))
        for argv in ([], ["--session=claude:none"],
                     ["--session=x", "--park-after=soon"]):
            done = subprocess.run(
                [sys.executable, "-m", "swe.remote.keeper", *argv], env=env,
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, discovery.EXIT_USAGE, argv)
            self.assertIn("keeper", done.stderr)


if __name__ == "__main__":
    unittest.main()
