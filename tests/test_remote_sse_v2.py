"""Sync v2 (remote/sync.py, its routes in hub.py, schema/session_v2.json).

The hub's HTTP server runs over a throwaway home with the session
runtime on and keepers on threads driving the scripted ``FakeAdapter``
(``KeeperHubCase``). A test reads a v2 stream the way a client does,
over HTTP, or drives a ``V2Stream`` itself where it has to decide when
the stream is read (overflow, the ping clock).

Every v2 frame a test reads is checked against the committed schema as
it is read. Nothing sleeps or polls: a test waits on the runtime's
listener feed, on a keeper's answer (``drain``), or on the stream it
reads. Every test stops its keepers in cleanup (``KeeperHubCase``).
"""

import http.client
import json
import unittest

from swe.remote import hub as hub_mod
from swe.remote import protocol, protocol_compat, schema, sync as sync_mod
from swe.remote.adapters import events as ev
from swe.remote.adapters import fake
from swe.remote.journal import Journal

from tests.test_remote_hub import HubBase
from tests.test_remote_hub_keeper import (
    OPTIONS, Feed, KeeperHubCase,
)
from tests.test_remote_keeper import WAIT, kill


def is_sync_state(frame: dict) -> bool:
    """The frame every catch-up ends with: a ``session.state`` that
    carries ``live[]`` (the journaled event of that type does not)."""
    return frame.get("type") == "session.state" \
        and "live" in frame.get("data", {})


def journaled(frames) -> list[int]:
    return [f["seq"] for f in frames if f.get("seq", 0) > 0]


class Client:
    """A v2 client that follows the documented rules and nothing else:
    the cursor is the highest seq seen, a state frame replaces the live
    text, and a delta is appended only at the offset the client holds."""

    def __init__(self, case, fetch) -> None:
        self.case = case
        self.fetch = fetch          # () -> the snapshot body
        self.cursor = 0
        self.epoch = None
        self.seqs: list[int] = []   # every journaled seq, as it arrived
        self.state = None
        self.live: dict[str, str] = {}
        self.stopped: set[str] = set()
        self.pending: set[str] = set()
        self.streamed: dict[str, str] = {}   # id -> text when it completed
        self.completed: dict[str, str] = {}
        self.resyncs = 0

    def feed(self, frames) -> None:
        for frame in frames:
            self.apply(frame)

    def load(self, body: dict) -> None:
        self.cursor = body["head"]
        self.epoch = body["epoch"]
        self.state = body["state"]
        self.live = {m["id"]: m["text"] for m in body["live"]}
        self.stopped.clear()
        self.pending = {p["data"]["request_id"] for p in body["pending"]}

    def apply(self, frame: dict) -> None:
        etype, data = frame["type"], frame["data"]
        if etype in ("ping", "session.row"):
            return
        if etype == "session.snapshot":
            self.load(data)
            return
        if etype == "message.delta":
            self.delta(data)
            return
        seq = frame["seq"]
        if seq > 0:
            # Never twice, never backwards.
            self.case.assertGreater(seq, self.cursor, frame)
            self.cursor = seq
            self.seqs.append(seq)
        if etype == "session.state":
            self.state = data["state"]
            if "live" in data:
                self.live = {m["id"]: m["text"] for m in data["live"]}
                self.stopped.clear()
                self.pending &= set(data["pending"])
                self.cursor = max(self.cursor, data["head"])
                self.epoch = data["epoch"]
        elif etype in ("message.completed", "thinking.completed"):
            self.streamed[data["id"]] = self.live.pop(data["id"], "")
            self.completed[data["id"]] = data["text"]
        elif etype == "tool.started":
            self.live.pop(data["call_id"], None)
        elif etype == "turn.ended":
            self.live.clear()
        elif etype in ("approval.requested", "input.requested"):
            self.pending.add(data["request_id"])
        elif etype == "approval.resolved":
            self.pending.discard(data["request_id"])

    def delta(self, data: dict) -> None:
        mid = data["id"]
        if mid in self.stopped:
            return
        held = self.live.get(mid, "")
        if data["offset"] == len(held):
            self.live[mid] = held + data["text"]
        elif data["offset"] + len(data["text"]) <= len(held):
            return  # behind: text the client already has
        else:
            # A hole. Drop it, stop appending, ask for the snapshot and
            # carry on from its live[].
            self.stopped.add(mid)
            self.resyncs += 1
            body = self.fetch()
            self.live = {m["id"]: m["text"] for m in body["live"]}
            self.stopped.clear()


class V2Case(KeeperHubCase):
    """A hub on keepers, with helpers that read v2 streams."""

    def setUp(self) -> None:
        # The clock the pings run on. It only moves when a test moves
        # it, so no stream pings unless a test is about pings.
        self.tick = fake.FakeClock(0.0)
        super().setUp()

    def hub(self, *args, **kwargs):
        srv = super().hub(*args, **kwargs)
        srv.state.sync.clock = self.tick
        srv.state.sync.now = self.clock
        return srv

    def kill_hub(self, srv) -> None:
        super().kill_hub(srv)
        # A crash takes the open streams with it.
        srv.state.sync.close()

    # -- frames ---------------------------------------------------------

    def check(self, value, name: str = "Frame"):
        self.assertEqual(schema.validate(value, name), [], value)
        return value

    def sse(self, key: str, *, v2: bool = True, last=None, epoch=None,
            header: bool = False, srv=None):
        """An open stream on ``key``: v2 by ``?v=2`` (or the header),
        else today's."""
        srv = srv or self.srv
        conn = http.client.HTTPConnection("127.0.0.1", srv.port,
                                          timeout=WAIT)
        self.addCleanup(conn.close)
        args = []
        headers = dict(self.auth())
        if v2 and header:
            headers[protocol.VERSION_HEADER] = "2"
        elif v2:
            args.append("v=2")
        if last is not None:
            args.append(f"last={last}")
        if epoch is not None:
            args.append(f"epoch={epoch}")
        conn.request("GET", f"/v1/sessions/{key}/events?" + "&".join(args),
                     headers=headers)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.v2 = v2
        resp.ids = []
        return resp

    def read(self, resp, until, what: str = "a frame") -> list[dict]:
        """Read frames up to the first that ``until`` accepts. v2
        frames are checked against the schema; ``resp.ids`` collects
        each frame's SSE id (None when it has no ``id:`` line)."""
        out: list[dict] = []
        sse_id = None
        while True:
            line = resp.readline()
            if not line:
                self.fail(f"the stream ended before {what}; saw "
                          f"{[f['type'] for f in out]}")
            if line.startswith(b"id: "):
                sse_id = int(line[4:])
            elif line.startswith(b"data: "):
                frame = json.loads(line[6:])
                if resp.v2:
                    self.check(frame)
                resp.ids.append(sse_id)
                sse_id = None
                out.append(frame)
                if until(frame):
                    return out

    def caught_up(self, resp) -> list[dict]:
        """A v2 stream's catch-up: everything up to its state frame."""
        return self.read(resp, is_sync_state, "the state frame")

    def snapshot(self, key: str, srv=None) -> dict:
        status, body = self.call("GET", f"/v1/sessions/{key}/snapshot",
                                 srv=srv)
        self.assertEqual(status, 200, body)
        return self.check(body, "Snapshot")

    def open(self, key: str, **kwargs) -> sync_mod.V2Stream:
        """A stream the test reads by hand."""
        stream = self.srv.state.sync.open(key, **kwargs)
        self.addCleanup(stream.close)
        return stream

    def pump(self, stream) -> list[dict]:
        """Everything ``stream`` has to send right now."""
        out: list[dict] = []
        while stream.waiting:
            out.extend(stream.next(block=False))
        return [self.check(f) for f in out]

    # -- sessions -------------------------------------------------------

    def streaming(self, *chunks: str, after=()):
        """Launch a session whose first reply is stepped by hand: one
        step per chunk, then its completion, then ``after``. Returns
        ``(key, adapter)`` with the turn open and nothing said yet."""
        chunks = chunks or ("Hel", "lo wo", "rld")
        steps, offset = [], 0
        for n, chunk in enumerate(chunks, 1):
            steps.append(fake.emit(
                "message.delta", id="a1", kind="assistant", text=chunk,
                seq=n, offset=offset, done=n == len(chunks)))
            offset += len(chunk)
        steps.append(fake.emit("message.completed", id="a1",
                               kind="assistant", text="".join(chunks)))
        self.plans.append({"autoplay": False,
                           "turns": [steps + list(after)]})
        key = self.launch()
        return key, self.adapters[0]

    def say(self, key: str, adapter, steps: int = 1) -> None:
        """Play ``steps`` steps and return once the hub has them."""
        for _ in range(steps):
            self.assertTrue(adapter.step())
        self.drain(key)

    def after(self, key: str, seq: int) -> list[int]:
        return [s for s in self.seqs(key) if s > seq]


# ---------------------------------------------------------------------


class TestHandshake(V2Case):
    def test_status_says_what_the_hub_speaks(self):
        status, data = self.srv.request("GET", "/v1/status")
        body = self.check(json.loads(data), "Status")
        self.assertEqual((status, body["ok"], body["v"], body["protocols"]),
                         (200, True, 1, [1, 2]))
        self.assertEqual(body["features"],
                         list(protocol.FEATURES) + ["session_runtime"])
        self.assertEqual(body["boot_id"], self.srv.state.boot_id)
        # A restart is a new boot id, which is how a client notices one.
        self.kill_hub(self.srv)
        self.srv = self.hub()
        again = json.loads(self.srv.request("GET", "/v1/status")[1])
        self.assertNotEqual(again["boot_id"], body["boot_id"])

    def test_a_v2_client_and_a_v1_client_side_by_side(self):
        key = self.launch([fake.say("hi there", chunks=["hi ", "there"])],
                          [fake.say("again", chunks=["ag", "ain"])])
        self.feed.event("turn.ended")
        self.drain(key)
        old = self.sse(key, v2=False)
        new = self.sse(key)
        by_header = self.sse(key, header=True)
        # Today's client: today's frames, to the letter.
        v1 = self.read(old, lambda f: f["type"] == "journal.caught-up")
        self.assertEqual({f.get("v", 1) for f in v1}, {1})
        self.assertEqual(
            {f["type"] for f in v1[:-1]},
            {"session.status", "message.appended", "command.result"})
        self.assertEqual(v1[-1]["seq"], self.seqs(key)[-1])
        # A v2 client with no cursor: one snapshot, then the state.
        for resp in (new, by_header):
            first = self.caught_up(resp)
            self.assertEqual([f["type"] for f in first],
                             ["session.snapshot", "session.state"])
            self.assertEqual({f["v"] for f in first}, {2})
            self.assertEqual(first[0]["data"]["head"], self.seqs(key)[-1])
            self.assertEqual(first[1]["data"]["live"], [])
        head = first[0]["data"]["head"]

        # A turn, live on all three.
        status, out = self.post(key, "message",
                                {"text": "more", "client_id": "m2"})
        self.assertEqual(status, 202, out)
        live1 = self.read(old, lambda f: f["type"] == "session.status"
                          and f["data"]["status"] == "done")
        self.assertEqual({f.get("v", 1) for f in live1}, {1})
        self.assertEqual(
            [f["data"]["text"] for f in live1
             if f["type"] == "message.delta"], ["ag", "ain"])
        self.assertTrue(all("offset" not in f["data"] for f in live1
                            if f["type"] == "message.delta"))
        self.assertIn("message.appended", [f["type"] for f in live1])
        self.assertFalse({f["type"] for f in live1} & {
            "turn.started", "turn.ended", "message.completed",
            "session.state", "ping"})
        for resp in (new, by_header):
            # The send's result is the last event of its turn.
            live2 = self.read(resp, lambda f: f["type"] == "command.result"
                              and f.get("command_id") == "m2")
            self.assertEqual({f["v"] for f in live2}, {2})
            # Every journaled event, once, in order, as it was logged.
            self.assertEqual(journaled(live2), self.after(key, head))
            by_seq = {e["seq"]: e for e in self.journal(key)}
            self.assertEqual([f for f in live2 if f.get("seq")],
                             [by_seq[s] for s in journaled(live2)])
            deltas = [f["data"] for f in live2
                      if f["type"] == "message.delta"]
            self.assertEqual([(d["text"], d["offset"]) for d in deltas],
                             [("ag", 0), ("ain", 2)])
            # Only a journaled event has an SSE id: Last-Event-ID never
            # lands on a delta.
            self.assertEqual(
                [i for i in resp.ids[2:] if i is not None],
                journaled(live2))
        self.assertEqual(new.ids[:2], [None, None])

    def test_anything_but_2_is_a_client_of_today(self):
        for args, headers in (
                ({}, {}), ({"v": ["1"]}, {}), ({"v": ["two"]}, {}),
                ({}, {protocol.VERSION_HEADER: "1"}),
                ({"v": ["1"]}, {protocol.VERSION_HEADER: "2"})):
            self.assertFalse(protocol.wants_v2(args, headers), (args, headers))
        self.assertTrue(protocol.wants_v2({"v": ["2"]}, {}))
        self.assertTrue(protocol.wants_v2({}, {protocol.VERSION_HEADER:
                                               " 2 "}))
        self.assertTrue(protocol.wants_v2({"v": ["2"]}, None))


class TestStatusWithoutTheRuntime(HubBase):
    def test_no_session_runtime_feature(self):
        status, data = self.srv.request("GET", "/v1/status")
        body = json.loads(data)
        self.assertEqual(schema.validate(body, "Status"), [])
        self.assertEqual(body["features"], [
            f for f in protocol.FEATURES
            if f not in protocol.STORE_FEATURES])
        self.assertNotIn("session_runtime", body["features"])


class TestCatchUp(V2Case):
    def session(self):
        key = self.launch([fake.say("one")],
                          [fake.tool("Bash", input={"command": "ls"},
                                     output="a b"),
                           fake.say("two", chunks=["t", "wo"])])
        self.feed.event("turn.ended")
        self.drain(key)
        first = self.caught_up(self.sse(key))
        state = first[-1]["data"]
        return key, state["head"], state["epoch"]

    def more(self, key: str) -> None:
        status, out = self.post(key, "message",
                                {"text": "more", "client_id": "m2"})
        self.assertEqual(status, 202, out)
        self.feed.event("turn.ended")
        self.drain(key)

    def test_a_small_gap_is_replayed(self):
        key, cursor, epoch = self.session()
        self.assertEqual((cursor, epoch), (self.seqs(key)[-1], 0))
        self.more(key)
        frames = self.caught_up(self.sse(key, last=cursor, epoch=epoch))
        # The events it missed, in order, and no snapshot.
        self.assertNotIn("session.snapshot", [f["type"] for f in frames])
        missed = self.after(key, cursor)
        self.assertGreater(len(missed), 5)
        self.assertEqual(journaled(frames), missed)
        self.assertEqual(frames[:-2], [e for e in self.journal(key)
                                       if e["seq"] > cursor])
        # Then the row as it is now, and the state.
        self.assertEqual(
            (frames[-2]["type"], frames[-2]["data"]["row"]["key"]),
            ("session.row", key))
        # Deltas are never replayed: they were never journaled.
        self.assertNotIn("message.delta", [f["type"] for f in frames])
        state = frames[-1]
        self.assertEqual(
            (state["seq"], state["data"]["head"], state["data"]["epoch"],
             state["data"]["state"], state["data"]["live"],
             state["data"]["pending"]),
            (0, missed[-1], 0, "idle", [], []))

    def test_a_client_that_is_up_to_date_gets_the_row_and_the_state(self):
        key, cursor, epoch = self.session()
        frames = self.caught_up(self.sse(key, last=cursor, epoch=epoch))
        self.assertEqual([f["type"] for f in frames],
                         ["session.row", "session.state"])
        self.assertEqual(frames[1]["data"]["head"], cursor)

    def test_last_event_id_is_the_cursor(self):
        key, cursor, epoch = self.session()
        self.more(key)
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port,
                                          timeout=WAIT)
        self.addCleanup(conn.close)
        conn.request("GET", f"/v1/sessions/{key}/events?v=2&epoch={epoch}",
                     headers={**self.auth(), "Last-Event-ID": str(cursor)})
        resp = conn.getresponse()
        resp.v2, resp.ids = True, []
        self.assertEqual(journaled(self.caught_up(resp)),
                         self.after(key, cursor))

    def test_a_wide_gap_gets_a_snapshot(self):
        key, cursor, epoch = self.session()
        self.more(key)
        self.srv.state.sync.replay_max = 3
        frames = self.caught_up(self.sse(key, last=cursor, epoch=epoch))
        self.assertEqual([f["type"] for f in frames],
                         ["session.snapshot", "session.state"])
        self.assertEqual(frames[0]["data"]["head"], self.seqs(key)[-1])
        self.assertEqual(frames[1]["data"]["head"], self.seqs(key)[-1])

    def test_another_epoch_gets_a_snapshot(self):
        key, cursor, epoch = self.session()
        self.more(key)
        for asked in (epoch + 1, None, "x"):
            frames = self.caught_up(self.sse(key, last=cursor, epoch=asked))
            self.assertEqual([f["type"] for f in frames],
                             ["session.snapshot", "session.state"], asked)
            self.assertEqual(frames[0]["data"]["epoch"], epoch)

    def test_a_cursor_of_zero_gets_a_snapshot(self):
        # A ring that was trimmed still reads "from the start" with
        # nothing missing, so a replay from 0 could not tell what it
        # left out. A client with no cursor has nothing to add to.
        key, cursor, epoch = self.session()
        self.assertLess(cursor, self.srv.state.sync.replay_max)
        frames = self.caught_up(self.sse(key, last=0, epoch=epoch))
        self.assertEqual([f["type"] for f in frames],
                         ["session.snapshot", "session.state"])
        self.assertEqual(frames[0]["data"]["head"], cursor)

    def test_a_cursor_past_the_journal_gets_a_snapshot(self):
        key, cursor, epoch = self.session()
        frames = self.caught_up(self.sse(key, last=cursor + 50, epoch=epoch))
        self.assertEqual([f["type"] for f in frames],
                         ["session.snapshot", "session.state"])
        self.assertEqual(frames[1]["data"]["head"], cursor)

    def test_the_epoch_moves_when_the_journal_starts_over(self):
        key, cursor, epoch = self.session()
        # A stream that is open when the hub learns the journal cannot
        # be made whole is sent a snapshot on the new epoch.
        resp = self.sse(key, last=cursor, epoch=epoch)
        self.caught_up(resp)
        self.assertEqual(self.srv.state.sync.new_epoch(key), 1)
        frames = self.caught_up(resp)
        self.assertEqual(
            [(f["type"], f["data"]["epoch"]) for f in frames],
            [("session.snapshot", 1), ("session.state", 1)])
        self.kill_hub(self.srv)
        # The journal now claims more than the keeper's log holds, so
        # the next hub drops it and takes the log again from seq 1.
        Journal(self.home).append_at(key, {"type": "stray", "seq": 900})
        self.feed = Feed(self)
        self.srv = self.hub()
        self.assertEqual(self.store().get_session(key).epoch, 2)
        # The same seqs now name another timeline: a cursor from the
        # old epoch is never replayed against it.
        self.assertLessEqual(cursor, self.seqs(key)[-1])
        frames = self.caught_up(self.sse(key, last=cursor, epoch=1))
        self.assertEqual(
            [(f["type"], f["data"]["epoch"]) for f in frames],
            [("session.snapshot", 2), ("session.state", 2)])
        # On the new epoch a replay works again.
        frames = self.caught_up(self.sse(key, last=cursor - 3, epoch=2))
        self.assertEqual(journaled(frames), self.after(key, cursor - 3))
        self.assertEqual(len(journaled(frames)), 3)

    def test_an_unknown_session_is_404(self):
        for route in ("events?v=2", "snapshot", "messages?v=2"):
            status, out = self.call("GET", f"/v1/sessions/claude:nope/{route}")
            self.assertEqual((status, out["error"]["code"]),
                             (404, "unknown_session"), route)
        status, _data = self.srv.request(
            "GET", "/v1/sessions/claude:nope/snapshot")
        self.assertEqual(status, 401)


class TestLiveText(V2Case):
    """``live[]``: the text of a reply in progress, after each kind of
    catch-up. Deltas are never journaled, so nothing else brings it."""

    LIVE = [{"id": "a1", "kind": "assistant", "text": "Hello wo",
             "turn_id": "t1"}]

    def test_after_a_replay(self):
        key, adapter = self.streaming()
        first = self.caught_up(self.sse(key))[-1]["data"]
        self.assertEqual(first["live"], [])
        self.say(key, adapter, 2)
        resp = self.sse(key, last=first["head"], epoch=first["epoch"])
        client = Client(self, lambda: self.snapshot(key))
        frames = self.caught_up(resp)
        client.feed(frames)
        # Nothing was journaled meanwhile: the row and the state frame
        # are all of it.
        self.assertEqual([f["type"] for f in frames],
                         ["session.row", "session.state"])
        self.assertEqual(frames[1]["data"]["live"], self.LIVE)
        self.assertEqual(frames[1]["data"]["state"], "running")
        # The reply carries on from exactly where live[] left off.
        self.say(key, adapter, 2)
        rest = self.read(resp, lambda f: f["type"] == "message.completed")
        client.feed(rest)
        self.assertEqual(
            [(f["data"]["text"], f["data"]["offset"]) for f in rest
             if f["type"] == "message.delta"], [("rld", 8)])
        self.assertEqual(client.streamed["a1"], "Hello world")
        self.assertEqual(client.completed["a1"], "Hello world")
        self.assertEqual(client.resyncs, 0)
        self.assertEqual(self.srv.state.sync.live(key), [])

    def test_after_a_snapshot(self):
        key, adapter = self.streaming()
        self.say(key, adapter, 2)
        resp = self.sse(key)
        frames = self.caught_up(resp)
        self.assertEqual([f["type"] for f in frames],
                         ["session.snapshot", "session.state"])
        self.assertEqual(frames[0]["data"]["live"], self.LIVE)
        self.assertEqual(frames[1]["data"]["live"], self.LIVE)
        self.assertEqual(frames[0]["data"]["open_turn"], "t1")
        client = Client(self, lambda: self.snapshot(key))
        client.feed(frames)
        self.say(key, adapter, 2)
        client.feed(self.read(
            resp, lambda f: f["type"] == "message.completed"))
        self.assertEqual(client.streamed["a1"], "Hello world")
        self.assertEqual(client.resyncs, 0)

    def test_a_delta_the_state_frame_holds_is_not_sent_again(self):
        key, adapter = self.streaming()
        # Subscribed, and the first chunk lands before the catch-up is
        # built: it is in the queue and in live[] both.
        stream = self.open(key)
        self.say(key, adapter)
        frames = [self.check(f) for f in stream.catch_up()]
        self.assertEqual(frames[-1]["data"]["live"][0]["text"], "Hel")
        self.assertEqual(self.pump(stream), [])
        self.say(key, adapter)
        sent = self.pump(stream)
        self.assertEqual(
            [(f["type"], f["data"]["text"], f["data"]["offset"])
             for f in sent], [("message.delta", "lo wo", 3)])

    def test_events_that_land_while_catching_up_come_before_the_state(self):
        key, adapter = self.streaming(after=[
            fake.tool("Bash", output="ok"),
            fake.emit("message.delta", id="a2", kind="assistant",
                      text="next", seq=1, offset=0)])
        first = self.caught_up(self.sse(key))[-1]["data"]
        self.say(key, adapter)
        stream = self.open(key, last=first["head"], epoch=first["epoch"])
        journal = self.srv.state.journal
        read = journal.read_since

        def read_then_the_agent_moves(*args, **kwargs):
            # The rest of the first reply, a tool call and the start of
            # the next reply land after the catch-up read the journal
            # and before its state frame is taken.
            out = read(*args, **kwargs)
            journal.read_since = read
            self.say(key, adapter, 5)
            return out

        journal.read_since = read_then_the_agent_moves
        frames = [self.check(f) for f in stream.catch_up()]
        client = Client(self, lambda: self.snapshot(key))
        client.live = {"a1": "Hel"}
        client.cursor, client.epoch = first["head"], first["epoch"]
        client.feed(frames)
        # Those events are sent before the state, whose live text is
        # true after them: no delta from before it, nothing cleared
        # after it.
        self.assertEqual(
            [f["type"] for f in frames],
            ["session.row", "message.completed", "tool.started",
             "tool.completed", "session.state"])
        self.assertEqual(frames[-1]["data"]["live"], [
            {"id": "a2", "kind": "assistant", "text": "next",
             "turn_id": "t1"}])
        self.assertEqual(frames[-1]["data"]["head"], self.seqs(key)[-1])
        self.assertEqual(client.live, {"a2": "next"})
        self.assertEqual(client.seqs, self.after(key, first["head"]))
        self.assertEqual(self.pump(stream), [])

    def test_a_welcome_s_text_is_sent_after_the_replay(self):
        key = self.launch([fake.say("x")])
        self.feed.event("turn.ended")
        self.drain(key)
        sync = self.srv.state.sync
        stream = self.open(key)
        client = Client(self, lambda: self.snapshot(key))
        client.feed(self.check(f) for f in stream.catch_up())
        head = client.cursor
        # The hub's connection to a keeper comes back. While it was
        # away a turn ended and the next one began a reply: the welcome
        # holds that reply, and the replay brings the older turn's end.
        sync.on_welcome(key, [{"id": "n1", "kind": "assistant",
                               "text": "new reply", "turn_id": "t2"}],
                        head + 2)
        self.assertEqual(self.pump(stream), [])
        for seq, event in enumerate((
                ev.turn_ended(key, "t1", "completed", ts=1.0),
                ev.turn_started(key, "t2", ts=1.0)), head + 1):
            sync.on_event(key, dict(event, seq=seq))
        sync.resync(key)
        frames = self.pump(stream)
        client.feed(frames)
        self.assertEqual([f["type"] for f in frames],
                         ["turn.ended", "turn.started", "session.state"])
        # The replayed turn.ended cleared nothing that was written
        # after it, on the hub or on the client.
        self.assertEqual(sync.live(key)[0]["text"], "new reply")
        self.assertEqual(client.live, {"n1": "new reply"})
        # An event from after the welcome does end it.
        sync.on_event(key, dict(ev.message_completed(
            key, "n1", "assistant", "new reply!", ts=1.0), seq=head + 3))
        self.assertEqual(sync.live(key), [])

    def test_a_reply_survives_a_hub_restart(self):
        key, adapter = self.streaming()
        client = Client(self, lambda: self.snapshot(key))
        resp = self.sse(key)
        client.feed(self.caught_up(resp))
        start = client.cursor
        self.say(key, adapter)
        client.feed(self.read(resp, lambda f: f["type"] == "message.delta"))
        self.assertEqual(client.live, {"a1": "Hel"})
        self.kill_hub(self.srv)
        # No hub: the agent writes on, and the keeper holds the text.
        self.assertTrue(adapter.step())
        self.feed = Feed(self)
        self.srv = self.hub()
        # The new hub never saw a delta; the keeper's welcome told it.
        self.assertEqual(self.srv.state.sync.live(key), self.LIVE)
        resp = self.sse(key, last=client.cursor, epoch=client.epoch)
        frames = self.caught_up(resp)
        client.feed(frames)
        self.assertNotIn("session.snapshot", [f["type"] for f in frames])
        self.assertEqual(client.live, {"a1": "Hello wo"})
        self.say(key, adapter, 2)
        client.feed(self.read(
            resp, lambda f: f["type"] == "message.completed"))
        self.assertTrue(adapter.step())
        client.feed(self.read(resp, lambda f: f["type"] == "session.state"
                              and f["data"]["state"] == "idle"))
        # No lost text, and no gap or repeat in what was journaled:
        # from the first snapshot on, the client took the journal.
        self.assertEqual(client.streamed["a1"], "Hello world")
        self.assertEqual(client.resyncs, 0)
        self.drain(key)
        self.assertEqual(client.seqs, self.after(key, start))
        self.assertGreaterEqual(len(client.seqs), 3)

    def test_a_keeper_reconnect_sends_the_state_again(self):
        key, adapter = self.streaming()
        resp = self.sse(key)
        self.caught_up(resp)
        self.say(key, adapter)
        self.read(resp, lambda f: f["type"] == "message.delta")
        # The hub's connection to the keeper drops and comes back: the
        # welcome's text replaces the hub's, and open streams get it.
        self.srv.state.runtime.session(key).client.close()
        frame = self.read(resp, is_sync_state)[-1]
        self.assertEqual(frame["data"]["live"][0]["text"], "Hel")


class TestDeltaOffsets(V2Case):
    def test_a_client_that_loses_a_delta_recovers_from_the_snapshot(self):
        chunks = ("Hel", "lo ", "wor", "ld")
        key, adapter = self.streaming(*chunks)
        client = Client(self, lambda: self.snapshot(key))
        resp = self.sse(key)
        client.feed(self.caught_up(resp))
        got = []
        for n in range(len(chunks)):
            self.say(key, adapter)
            delta = self.read(
                resp, lambda f: f["type"] == "message.delta")[-1]
            got.append(delta["data"])
            if n == 1:
                continue  # lost on the way to this client
            client.apply(delta)
            if n == 2:
                # Its offset did not match: dropped, the snapshot
                # asked for, and the text taken from live[].
                self.assertEqual(client.resyncs, 1)
                self.assertEqual(client.live, {"a1": "Hello wor"})
        # Every delta says where its text starts, and they join up.
        self.assertEqual([d["offset"] for d in got], [0, 3, 6, 9])
        self.assertEqual([d["seq"] for d in got], [1, 2, 3, 4])
        self.assertEqual(client.live, {"a1": "Hello world"})
        # One that arrives again is behind the text held: ignored.
        client.delta(got[2])
        self.assertEqual((client.live["a1"], client.resyncs),
                         ("Hello world", 1))
        self.say(key, adapter)
        client.feed(self.read(
            resp, lambda f: f["type"] == "message.completed"))
        self.assertEqual(client.streamed["a1"], client.completed["a1"])

    def test_live_text_nothing_completes_is_bounded(self):
        key = self.launch([fake.say("x")])
        self.feed.event("turn.ended")
        self.drain(key)
        sync = self.srv.state.sync
        for n in range(sync_mod.LIVE_PER_SESSION_MAX + 5):
            sync.on_event(key, ev.message_delta(
                key, f"m{n}", "assistant", "t", 1, offset=0))
        ids = [m["id"] for m in sync.live(key)]
        self.assertEqual(len(ids), sync_mod.LIVE_PER_SESSION_MAX)
        self.assertEqual((ids[0], ids[-1]), ("m5", "m36"))
        # A session at rest holds none.
        sync.on_event(key, ev.session_state(key, "idle", ts=1.0))
        self.assertEqual(sync.live(key), [])

    def test_the_hub_counts_the_offset_an_adapter_leaves_out(self):
        key = self.launch([fake.say("x")])
        self.feed.event("turn.ended")
        self.drain(key)
        sync = self.srv.state.sync
        stream = self.open(key)
        stream.catch_up()
        for n, (text, offset) in enumerate(
                [("ab", None), ("cd", None), ("ef", 99), ("Z", 5)], 1):
            sync.on_event(key, ev.message_delta(
                key, "m9", "assistant", text, n, offset=offset))
        sent = self.pump(stream)
        # None: counted. Past the hub's text: put where the hub's text
        # ends. Inside it: sent as the adapter said (the text is cut
        # there), and a client takes the snapshot.
        self.assertEqual([(f["data"]["text"], f["data"]["offset"])
                          for f in sent],
                         [("ab", 0), ("cd", 2), ("ef", 4), ("Z", 5)])
        self.assertEqual(sync.live(key), [
            {"id": "m9", "kind": "assistant", "text": "abcdeZ"}])


class TestOverflow(V2Case):
    def test_a_full_queue_is_read_again_from_the_journal(self):
        tools = [fake.tool("Bash", input={"command": f"echo {n}"},
                           output=str(n)) for n in range(6)]
        key, adapter = self.streaming(
            "Hel", "lo", after=tools + [
                fake.emit("message.delta", id="a2", kind="assistant",
                          text="still ", seq=1, offset=0),
                fake.emit("message.delta", id="a2", kind="assistant",
                          text="going", seq=2, offset=6),
                fake.emit("message.completed", id="a2", kind="assistant",
                          text="still going")])
        sync = self.srv.state.sync
        sync.queue_max = 4
        client = Client(self, lambda: self.snapshot(key))
        stream = self.open(key)
        client.feed(self.check(f) for f in stream.catch_up())
        cursor = client.cursor
        # The stream is not read while the agent says a whole reply,
        # runs six tools and starts the next reply.
        self.say(key, adapter, 3 + 6 + 1)
        self.assertTrue(stream.overflow)
        frames = self.pump(stream)
        client.feed(frames)
        self.assertFalse(stream.overflow)
        # Every journaled event since, once, in order: none lost.
        missed = self.after(key, cursor)
        self.assertEqual(len(missed), 1 + 12)
        self.assertEqual(journaled(frames), missed)
        self.assertEqual(client.seqs, missed)
        # Then the row and the state, with the text in progress.
        self.assertEqual([f["type"] for f in frames[-2:]],
                         ["session.row", "session.state"])
        self.assertEqual(frames[-1]["data"]["live"], [
            {"id": "a2", "kind": "assistant", "text": "still ",
             "turn_id": "t1"}])
        self.assertEqual(frames[-1]["data"]["head"], missed[-1])
        self.assertEqual(client.live, {"a2": "still "})
        # And it is live again, from exactly there.
        self.say(key, adapter, 2)
        rest = self.pump(stream)
        client.feed(rest)
        self.assertEqual(
            [(f["type"], f["data"].get("offset")) for f in rest],
            [("message.delta", 6), ("message.completed", None)])
        self.assertEqual(client.streamed["a2"], "still going")
        self.assertEqual(client.resyncs, 0)
        self.assertEqual(client.seqs, self.after(key, cursor))

    def test_an_overflow_wider_than_a_replay_gets_a_snapshot(self):
        tools = [fake.tool("Bash", output=str(n)) for n in range(7)]
        key, adapter = self.streaming("a", after=tools)
        sync = self.srv.state.sync
        sync.queue_max, sync.replay_max = 2, 5
        stream = self.open(key)
        stream.catch_up()
        self.say(key, adapter, 2 + 6)
        frames = self.pump(stream)
        self.assertEqual([f["type"] for f in frames],
                         ["session.snapshot", "session.state"])
        self.assertEqual(stream.sent, self.seqs(key)[-1])
        # Nothing after it is sent twice.
        self.assertTrue(adapter.step())
        self.drain(key)
        later = journaled(self.pump(stream))
        self.assertEqual(later, self.after(key, frames[0]["data"]["head"]))
        self.assertEqual(len(later), 2)


class TestPing(V2Case):
    def test_a_ping_every_ten_seconds(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        self.drain(key)
        head = self.seqs(key)[-1]
        stream = self.open(key)
        stream.catch_up()
        self.assertEqual(protocol.PING_S, 10.0)
        self.tick.advance(9.5)
        self.assertEqual(stream.next(block=False), [])
        self.tick.advance(0.5)
        ping = self.check(stream.next(block=False)[0])
        self.assertEqual(ping, {
            "v": 2, "type": "ping", "session": key, "ts": self.clock(),
            "data": {"ts": self.clock(), "head": head}})
        self.assertNotIn("seq", ping)
        # Not again until ten more seconds have passed.
        self.assertEqual(stream.next(block=False), [])
        self.tick.advance(9.9)
        self.assertEqual(stream.next(block=False), [])
        self.tick.advance(0.1)
        self.assertEqual(stream.next(block=False)[0]["type"], "ping")
        # A long silence is one ping, not one per interval missed.
        self.tick.advance(45)
        self.assertEqual(stream.next(block=False)[0]["type"], "ping")
        self.assertEqual(stream.next(block=False), [])

    def test_a_v2_stream_is_pinged_and_a_v1_stream_is_not(self):
        key = self.launch([fake.say("hi")], [fake.say("more")])
        self.feed.event("turn.ended")
        self.drain(key)
        new = self.sse(key)
        old = self.sse(key, v2=False)
        self.caught_up(new)
        self.read(old, lambda f: f["type"] == "journal.caught-up")
        sync = self.srv.state.sync
        for _ in range(2):
            self.tick.advance(protocol.PING_S)
            sync.wake()
            frames = self.read(new, lambda f: f["type"] == "ping")
            self.assertEqual([f["type"] for f in frames], ["ping"])
            self.assertEqual(frames[0]["data"]["head"], self.seqs(key)[-1])
        self.assertEqual(new.ids[-1], None)
        # The ping's head follows what the stream has sent.
        self.assertEqual(self.post(key, "message", {
            "text": "go", "client_id": "p1"})[0], 202)
        turn = self.read(new, lambda f: f["type"] == "command.result"
                         and f.get("command_id") == "p1")
        self.drain(key)
        self.assertEqual(turn[-1]["seq"], self.seqs(key)[-1])
        self.tick.advance(protocol.PING_S)
        sync.wake()
        ping = self.read(new, lambda f: f["type"] == "ping")
        self.assertEqual([f["type"] for f in ping], ["ping"])
        self.assertEqual(ping[0]["data"]["head"], turn[-1]["seq"])
        # Today's stream saw the turn and no ping.
        seen = self.read(old, lambda f: f["type"] == "session.status"
                         and f["data"]["status"] == "done")
        self.assertNotIn("ping", [f["type"] for f in seen])

    def test_the_hub_stopping_ends_the_stream(self):
        key = self.launch([fake.say("hi")])
        self.feed.event("turn.ended")
        self.drain(key)
        resp = self.sse(key)
        self.caught_up(resp)
        stream = self.open(key)
        self.srv.state.sync.close()
        self.assertIsNone(stream.next(block=False))
        # Nothing more is written, and the connection closes.
        self.assertEqual(resp.read().strip(), b"")


class TestSnapshot(V2Case):
    PLAN = [{"text": "look", "status": "completed"},
            {"text": "fix", "status": "in_progress"}]
    FILES = [{"path": "a.py", "kind": "modify", "added": 3, "removed": 1}]

    def busy(self):
        """A session in the middle of a turn, with something of every
        kind a snapshot carries."""
        self.plans.append({"autoplay": False, "turns": [[
            fake.emit("plan.updated", items=self.PLAN),
            fake.task("sub1", title="look around"),
            fake.task("sub2", title="done already"),
            fake.task("sub2", "completed", title="done already"),
            fake.emit("checkpoint.created", checkpoint_id="cp1", files=2),
            fake.tool("Edit", input={"path": "a.py"}, output="ok"),
            fake.emit("changes.updated", files=self.FILES),
            fake.emit("usage.updated", input_tokens=10, output_tokens=5),
            fake.emit("message.delta", id="a1", kind="assistant",
                      text="about to ", seq=1, offset=0),
            fake.ask("run it?", kind="command", options=OPTIONS,
                     request_id="r1"),
        ]]})
        key = self.launch(prompt="fix the bug")
        adapter = self.adapters[0]
        self.say(key, adapter, 10)
        status, out = self.post(key, "message",
                                {"text": "and then this", "client_id": "q1"})
        self.assertEqual((status, out["receipt"]), (202, "queued"))
        self.feed.event("queue.updated")
        self.drain(key)
        return key

    def test_the_snapshot_route(self):
        key = self.busy()
        body = self.snapshot(key)
        self.assertEqual(
            (body["session"], body["epoch"], body["head"], body["boot_id"],
             body["state"], body["open_turn"], body["has_more"],
             body["next_before"]),
            (key, 0, self.seqs(key)[-1], self.srv.state.boot_id,
             "waiting", "t1", False, None))
        row = body["row"]
        self.assertEqual(
            (row["key"], row["ownership"], row["mode"], row["control"],
             row["state"], row["unread"]),
            (key, "attached", "keeper", "full", "waiting", False))
        self.assertTrue(body["caps"]["steer"])
        # The open request as the event that asked, the queue, the text
        # in progress.
        self.assertEqual(
            [(p["type"], p["data"]["request_id"], p["data"]["prompt"],
              [o["id"] for o in p["data"]["options"]])
             for p in body["pending"]],
            [("approval.requested", "r1", "run it?",
              ["allow", "always", "deny"])])
        self.assertEqual([(q["command_id"], q["text"])
                          for q in body["queue"]], [("q1", "and then this")])
        self.assertEqual(body["live"], [
            {"id": "a1", "kind": "assistant", "text": "about to ",
             "turn_id": "t1"}])
        # What the journal adds up to.
        self.assertEqual(body["plan"], {"items": self.PLAN})
        self.assertEqual(
            [(t["task_id"], t["status"], t["title"]) for t in body["tasks"]],
            [("sub1", "running", "look around")])
        self.assertEqual(
            [(c["checkpoint_id"], c["files"], c["turn_id"])
             for c in body["checkpoints"]], [("cp1", 2, "t1")])
        self.assertIn(body["checkpoints"][0]["seq"], self.seqs(key))
        self.assertEqual(body["changes"], self.FILES)
        self.assertEqual(body["usage"],
                         {"input_tokens": 10, "output_tokens": 5})
        # The timeline: what a view draws, and none of the bookkeeping.
        types = [m["type"] for m in body["messages"]]
        self.assertEqual(types, [
            "turn.started", "message.completed", "plan.updated",
            "task.updated", "task.updated", "task.updated",
            "checkpoint.created", "tool.started", "tool.completed",
            "approval.requested"])
        self.assertEqual(body["messages"][1]["data"]["text"], "fix the bug")
        by_seq = {e["seq"]: e for e in self.journal(key)}
        self.assertEqual(body["messages"],
                         [by_seq[m["seq"]] for m in body["messages"]])

    def test_the_stream_sends_the_same_body(self):
        key = self.busy()
        body = self.snapshot(key)
        frames = self.caught_up(self.sse(key))
        self.assertEqual(frames[0]["type"], "session.snapshot")
        self.assertEqual(frames[0]["data"], body)
        self.assertNotIn("seq", frames[0])
        state = frames[1]["data"]
        self.assertEqual(
            (state["state"], state["pending"], state["head"],
             state["live"]),
            ("waiting", ["r1"], body["head"], body["live"]))

    def test_the_last_messages_and_the_pages_before_them(self):
        key = self.busy()
        self.srv.state.sync.snapshot_messages = 3
        body = self.snapshot(key)
        timeline = [e for e in self.journal(key)
                    if e["type"] in sync_mod.TIMELINE_TYPES]
        self.assertEqual(body["messages"], timeline[-3:])
        self.assertEqual((body["has_more"], body["next_before"]),
                         (True, timeline[-3]["seq"]))
        # Older pages come from the messages route, as v2 events.
        seen, before = list(body["messages"]), body["next_before"]
        while before is not None:
            status, page = self.call(
                "GET", f"/v1/sessions/{key}/messages?v=2&limit=4"
                       f"&before={before}")
            self.assertEqual(status, 200, page)
            self.check(page, "MessagesPage")
            self.assertLessEqual(len(page["messages"]), 4)
            seen = page["messages"] + seen
            before = page["next_before"]
            self.assertEqual(page["has_more"], before is not None)
        self.assertEqual(seen, timeline)
        # Today's route is unchanged: v1 messages.
        status, old = self.call("GET", f"/v1/sessions/{key}/messages")
        self.assertEqual({m["type"] for m in old["messages"]},
                         {"message.appended"})
        self.assertEqual({m["v"] for m in old["messages"]}, {1})

    def test_the_fold_carries_on_from_where_it_stopped(self):
        key = self.busy()
        first = self.snapshot(key)
        sync = self.srv.state.sync
        fold = sync._folds[key]
        self.assertEqual(fold.head, first["head"])
        status, out = self.post(key, "approval", {
            "request_id": "r1", "decision": "allow"})
        self.assertEqual(status, 200, out)
        self.adapters[0].play()
        self.feed.event("turn.ended")
        self.drain(key)
        second = self.snapshot(key)
        self.assertIs(sync._folds[key], fold)
        self.assertGreater(second["head"], first["head"])
        self.assertEqual(
            (second["pending"], second["live"], second["plan"],
             [c["checkpoint_id"] for c in second["checkpoints"]]),
            ([], [], {"items": self.PLAN}, ["cp1"]))
        self.assertIn("approval.resolved",
                      [m["type"] for m in second["messages"]])

    def test_a_lost_keeper_is_in_the_state_and_the_snapshot(self):
        key = self.busy()
        resp = self.sse(key)
        client = Client(self, lambda: self.snapshot(key))
        client.feed(self.caught_up(resp))
        self.assertEqual((client.pending, client.live),
                         ({"r1"}, {"a1": "about to "}))
        cut = self.seqs(key)[-1]
        kill(self.keepers[0])
        # Nothing is journaled for a keeper that is gone. The stream
        # is sent the state whole: nothing open, nothing being written.
        frames = self.read(
            resp, lambda f: is_sync_state(f)
            and f["data"]["state"] == "agent_down")
        client.feed(frames)
        self.assertEqual(
            (frames[-1]["seq"], frames[-1]["data"]["pending"],
             frames[-1]["data"]["live"], frames[-1]["data"]["head"]),
            (0, [], [], cut))
        self.assertEqual((client.pending, client.live, client.state),
                         (set(), {}, "agent_down"))
        self.assertEqual(self.seqs(key)[-1], cut)
        body = self.snapshot(key)
        self.assertEqual(
            (body["state"], body["pending"], body["queue"], body["live"],
             body["open_turn"]), ("agent_down", [], [], [], None))


    def test_a_lost_keeper_s_text_goes_with_nobody_watching(self):
        from tests.test_remote_hub_keeper import Stream

        key = self.busy()
        sync = self.srv.state.sync
        self.assertEqual(sync.watching(), 0)
        self.assertEqual([m["id"] for m in sync.live(key)], ["a1"])
        told = Stream(self, self.srv.state, key)   # a v1 subscriber
        kill(self.keepers[0])
        told.frame("session.status", status="error")
        self.assertEqual(sync.live(key), [])
        self.assertEqual(self.snapshot(key)["live"], [])


class TestSessionsNoKeeperHolds(HubBase):
    """A terminal session's journal holds v1 events. A v2 client reads
    it as v2 all the same."""

    KEY = "claude:s1"

    def setUp(self) -> None:
        super().setUp()
        self.state = self.srv.state
        self.mid = self.state.machine_id
        self._headers = None

    def headers(self):
        if self._headers is None:
            self._headers = self.auth()
        return self._headers

    def emit(self, etype: str, data: dict) -> dict:
        return self.state.emit(self.KEY, protocol.event(
            etype, self.KEY, self.mid, data))

    def pump(self, stream) -> list[dict]:
        out: list[dict] = []
        while stream.waiting:
            out.extend(stream.next(block=False))
        for frame in out:
            self.assertEqual(schema.validate(frame), [], frame)
        return out

    def tick_ping(self, stream, head: int) -> None:
        sync = self.state.sync
        sync.clock = lambda: 1e9
        self.assertEqual(stream.next(block=False)[0]["data"]["head"], head)

    def history(self) -> int:
        """Five v1 events; returns the seq before the first (the
        tailer may have journaled something of its own)."""
        base = self.state.journal.last_seq(self.KEY)
        self.emit("message.appended", protocol.message("user", "hello"))
        self.emit("message.appended", {
            **protocol.message("thinking", "hmm"), "duration_s": 1.5})
        self.emit("message.appended", protocol.message("tool_use", "", tool={
            "name": "Bash", "call_id": "c1", "args_summary": "Bash · ls",
            "args_full": '{"command": "ls"}'}))
        self.emit("message.appended", protocol.message(
            "tool_result", "a b", result_ref="c1"))
        self.emit("session.status", {"status": "active"})
        return base

    def test_a_v1_journal_is_served_as_v2(self):
        base = self.history()
        status, data = self.srv.request(
            "GET", f"/v1/sessions/{self.KEY}/snapshot",
            headers=self.headers())
        body = json.loads(data)
        self.assertEqual((status, schema.validate(body, "Snapshot")),
                         (200, []))
        mine = [m for m in body["messages"] if m["seq"] > base]
        self.assertEqual(
            [(m["type"], m["seq"] - base) for m in mine],
            [("message.completed", 1), ("thinking.completed", 2),
             ("tool.started", 3), ("tool.completed", 4)])
        self.assertEqual(
            (mine[0]["data"], mine[2]["data"], mine[3]["data"]),
            ({"id": f"j{base + 1}", "kind": "user", "text": "hello"},
             {"call_id": "c1", "name": "Bash", "summary": "Bash · ls",
              "input": '{"command": "ls"}'},
             {"call_id": "c1", "status": "ok", "output": "a b"}))
        self.assertEqual(
            (body["head"], body["epoch"], body["state"], body["caps"],
             body["row"]["ownership"]),
            (base + 5, 0, "running", None, "observed"))

    def test_live_events_and_text_arrive_as_v2(self):
        head = self.history() + 5
        stream = self.state.sync.open(self.KEY, last=head, epoch=0)
        self.addCleanup(stream.close)
        frames = stream.catch_up()
        self.assertEqual([f["type"] for f in frames],
                         ["session.row", "session.state"])
        # v1 deltas say no offset: the hub counts it.
        for n, text in enumerate(["Hi ", "there"], 1):
            self.state.publish_transient(self.KEY, protocol.delta_event(
                self.KEY, self.mid, "x1", "assistant", text, n,
                done=n == 2))
        deltas = self.pump(stream)
        self.assertEqual(
            [(f["type"], f["data"]["id"], f["data"]["text"],
              f["data"]["offset"], "seq" in f) for f in deltas],
            [("message.delta", "x1", "Hi ", 0, False),
             ("message.delta", "x1", "there", 3, False)])
        self.assertEqual(self.state.sync.live(self.KEY), [
            {"id": "x1", "kind": "assistant", "text": "Hi there",
             "done": True}])
        # The message that completes it takes the live text's id, so a
        # client retires that text when it lands.
        self.emit("message.appended",
                  protocol.message("assistant", "Hi there"))
        self.emit("approval.requested", {
            "request_id": "r1", "prompt": "run rm?",
            "options": [{"n": "1", "label": "Yes", "kind": "allow"},
                        {"n": "2", "label": "No"}]})
        self.emit("approval.resolved", {"request_id": "r1",
                                        "source": "gone"})
        self.emit("session.status", {"status": "done"})
        got = self.pump(stream)
        self.assertEqual(
            [(f["type"], f["seq"] - head) for f in got],
            [("message.completed", 1), ("approval.requested", 2),
             ("approval.resolved", 3), ("session.state", 4)])
        self.assertEqual(got[0]["data"],
                         {"id": "x1", "kind": "assistant",
                          "text": "Hi there"})
        self.assertEqual(got[1]["data"], {
            "request_id": "r1", "kind": "tool", "prompt": "run rm?",
            "options": [{"id": "1", "label": "Yes", "kind": "allow"},
                        {"id": "2", "label": "No"}]})
        self.assertEqual(got[2]["data"],
                         {"request_id": "r1", "source": "harness"})
        self.assertEqual(got[3]["data"], {"state": "idle"})
        self.assertEqual(self.state.sync.live(self.KEY), [])
        # A change to the row is a row frame, then the state.
        self.emit("session.ownership", {"ownership": "observed",
                                        "tmux": None, "mode": None,
                                        "kept": False})
        row = self.pump(stream)
        self.assertEqual([f["type"] for f in row],
                         ["session.row", "session.state"])
        self.assertEqual(row[0]["data"]["row"]["key"], self.KEY)
        # The ownership event has no v2 frame; the head covers it.
        self.assertEqual(row[1]["data"]["head"], head + 5)
        self.tick_ping(stream, head + 5)

    def test_a_tool_message_ends_the_text_streamed_for_its_call(self):
        head = self.history() + 5
        stream = self.state.sync.open(self.KEY, last=head, epoch=0)
        self.addCleanup(stream.close)
        stream.catch_up()
        # The ACP channel streams a tool's title under its own id.
        for call in ("c7", "c8"):
            self.state.publish_transient(self.KEY, protocol.delta_event(
                self.KEY, self.mid, f"acp:ab12:3:tool:{call}", "tool_use",
                f"Read {call}", 1))
        self.assertEqual(len(self.pump(stream)), 2)
        self.emit("message.appended", protocol.message("tool_use", "", tool={
            "name": "Read", "call_id": "c8", "args_summary": "Read c8",
            "args_full": ""}))
        frames = self.pump(stream)
        # The event carries the call's id, not the stream's: the state
        # frame after it is what retires the text on a client.
        self.assertEqual([f["type"] for f in frames],
                         ["tool.started", "session.state"])
        self.assertEqual(frames[0]["data"]["call_id"], "c8")
        self.assertEqual([m["id"] for m in frames[1]["data"]["live"]],
                         ["acp:ab12:3:tool:c7"])
        self.assertEqual([m["id"] for m in self.state.sync.live(self.KEY)],
                         ["acp:ab12:3:tool:c7"])

    def test_an_open_v2_stream_keeps_the_hub_out_of_low_power(self):
        # No v1 stream, no recent request: only the v2 stream says
        # someone is watching.
        self.state._client_seen_at = float("-inf")
        self.assertFalse(self.state.watched())
        stream = self.state.sync.open(self.KEY)
        self.assertTrue(self.state.watched())
        stream.close()
        self.assertFalse(self.state.watched())

    def test_today_s_stream_is_untouched(self):
        self.history()
        stream = self.state.bus.subscribe(self.KEY)
        self.addCleanup(self.state.bus.unsubscribe, stream)
        written = self.emit("message.appended",
                            protocol.message("assistant", "v1 still"))
        self.assertEqual(stream.get(timeout=WAIT), written)
        self.assertEqual(written["v"], 1)


class TestToV2(unittest.TestCase):
    """``protocol_compat.to_v2``: one v1 event as its v2 event."""

    KEY = "codex:t1"

    def v1(self, etype: str, data: dict, seq: int = 7) -> dict:
        event = protocol.event(etype, self.KEY, "mac", data, ts=5.0)
        event["seq"] = seq
        return event

    def conv(self, etype: str, data: dict, **kwargs) -> dict | None:
        out = protocol_compat.to_v2(self.v1(etype, data), **kwargs)
        if out is not None:
            self.assertEqual(ev.validate(out), [], out)
            self.assertEqual(schema.validate(out, "Event"), [], out)
            self.assertEqual((out["v"], out["session"], out["seq"],
                              out["ts"]), (2, self.KEY, 7, 5.0))
        return out

    def test_messages(self):
        for kind, want in (("user", "user"), ("assistant", "assistant"),
                           ("system", "system"), ("approval", "system")):
            out = self.conv("message.appended", protocol.message(kind, "x"))
            self.assertEqual((out["type"], out["data"]), (
                "message.completed", {"id": "j7", "kind": want, "text": "x"}))
        out = self.conv("message.appended", protocol.message("assistant", "x"),
                        msg_id="live9")
        self.assertEqual(out["data"]["id"], "live9")
        out = self.conv("message.appended", {
            **protocol.message("thinking", "hm"), "duration_s": 2})
        self.assertEqual((out["type"], out["data"]), (
            "thinking.completed", {"id": "j7", "text": "hm",
                                   "duration_s": 2}))
        out = self.conv("message.appended", protocol.message("tool_use", ""))
        self.assertEqual((out["type"], out["data"]),
                         ("tool.started", {"call_id": "j7", "name": "tool"}))
        out = self.conv("message.appended",
                        protocol.message("tool_result", "out"))
        self.assertEqual(out["data"], {"call_id": "j7", "status": "ok",
                                       "output": "out"})
        out = self.conv("message.appended",
                        protocol.message("compaction", "sum"))
        self.assertEqual((out["type"], out["data"]),
                         ("context.compacted", {"summary": "sum"}))
        out = self.conv("message.appended", protocol.message("error", "bad"))
        self.assertEqual((out["type"], out["data"]),
                         ("error", {"code": "error", "message": "bad"}))
        self.assertIsNone(self.conv("message.appended",
                                    protocol.message("mystery", "x")))

    def test_state_requests_results(self):
        for status, state in protocol_compat.STATE_FOR_STATUS.items():
            out = self.conv("session.status", {"status": status})
            self.assertEqual(out["data"], {"state": state})
            # And back: a v1 client of that v2 event sees the status.
            self.assertEqual(
                protocol_compat.to_v1(out)[0]["data"]["status"], status)
        self.assertIsNone(self.conv("session.status", {"status": "odd"}))
        out = self.conv("approval.requested", {
            "request_id": "r1", "prompt": "ok?", "auto": True,
            "options": [{"n": "1", "label": "Yes", "standing": True}]})
        self.assertEqual(out["data"], {
            "request_id": "r1", "kind": "tool", "prompt": "ok?",
            "auto": True, "options": [
                {"id": "1", "label": "Yes", "standing": True}]})
        self.assertIsNone(self.conv("approval.requested", {"prompt": "?"}))
        for data, want in (
                ({"request_id": "r1", "decision": "allow"},
                 {"request_id": "r1", "source": "user", "decision": "allow"}),
                ({"request_id": "r1", "source": "superseded"},
                 {"request_id": "r1", "source": "superseded"}),
                ({"request_id": "r1", "source": "keeper_lost",
                  "decision": "3"},
                 {"request_id": "r1", "source": "harness"})):
            self.assertEqual(self.conv("approval.resolved", data)["data"],
                             want)
        self.assertIsNone(self.conv("approval.resolved", {}))
        out = self.conv("command.result", {
            "command_id": "c1", "client_id": "m1", "op": "message",
            "ok": True, "note": "", "degraded": False})
        self.assertEqual((out["type"], out["command_id"], out["data"]),
                         ("command.result", "c1", {"op": "send", "ok": True}))
        out = self.conv("command.result", {
            "client_id": "m1", "op": "approval", "ok": False,
            "note": "gone", "request_id": "r1", "cancelled": True})
        self.assertEqual((out["command_id"], out["data"]), ("m1", {
            "op": "respond", "ok": False, "note": "gone",
            "cancelled": True, "request_id": "r1"}))
        self.assertIsNone(self.conv("command.result", {"op": "x", "ok": True}))
        out = self.conv("notify", {"kind": "finished", "status": "done"})
        self.assertEqual(out["data"], {"kind": "finished", "message": "",
                                       "status": "done"})
        self.assertIsNone(self.conv("notify", {"kind": "github"}))

    def test_the_rest(self):
        for etype in ("session.ownership", "session.meta",
                      "session.launched", "machine.online", "nonsense"):
            self.assertIsNone(self.conv(etype, {}), etype)
        self.assertIsNone(protocol_compat.to_v2("not an event"))
        v2 = ev.session_state(self.KEY, "idle")
        self.assertIs(protocol_compat.to_v2(v2), v2)
        # An event with no seq keeps none a client could take as a
        # cursor, and a delta has no seq at all.
        hub_made = protocol.event("notify", self.KEY, "mac",
                                  {"kind": "error"})
        del hub_made["seq"]
        self.assertEqual(protocol_compat.to_v2(hub_made)["seq"], 0)
        delta = protocol_compat.to_v2(protocol.delta_event(
            self.KEY, "mac", "x1", "assistant", "hi", 1), offset=4)
        self.assertEqual(ev.validate(delta), [])
        self.assertNotIn("seq", delta)
        self.assertEqual(delta["data"], {
            "id": "x1", "kind": "assistant", "text": "hi", "seq": 1,
            "offset": 4, "done": False})


# One valid event of every type, for the schema checks below.
K = "claude:x"
SAMPLES = {
    "session.state": ev.session_state(K, "idle", reason="r",
                                      harness_session_id="h1"),
    "session.caps": ev.session_caps(K, {
        "steer": True, "plan_mode": False, "rollback": False, "fork": True,
        "stop_task": False, "options": ["model"]}),
    "session.options": ev.session_options(K, {
        "model": {"value": "haiku", "mid_turn": "unverified",
                  "choices": [{"id": "haiku", "label": "Haiku",
                               "description": "fast"}],
                  "reported": "claude-haiku-5-5"},
        "effort": {"value": "", "mid_turn": "next_turn"},
        "permission_mode": {"value": "default", "mid_turn": "unverified"},
        "plan_mode": {"value": False, "mid_turn": "unverified"}}),
    "turn.started": ev.turn_started(K, "t1", by="phone", text="go"),
    "turn.ended": ev.turn_ended(K, "t1", "completed", duration_s=1.5),
    "message.delta": ev.message_delta(K, "m1", "assistant", "hi", 1,
                                      offset=0, turn_id="t1"),
    "message.completed": ev.message_completed(
        K, "m1", "user", "hi", by="phone", attachments=["/tmp/a.png"]),
    "thinking.completed": ev.thinking_completed(K, "m2", "hm",
                                                duration_s=2),
    "tool.started": ev.tool_started(K, "c1", "Bash", summary="Bash · ls",
                                    input={"command": "ls"}, task_id="s1"),
    "tool.updated": ev.tool_updated(K, "c1", text="a", summary="s"),
    "tool.completed": ev.tool_completed(
        K, "c1", "ok", name="Edit", output="done", file_edits=[
            {"path": "a.py", "kind": "modify", "diff": "@@",
             "old_path": "b.py"}]),
    "approval.requested": ev.approval_requested(
        K, "r1", "command", "run?", options=OPTIONS, call_id="c1",
        auto=False),
    "approval.resolved": ev.approval_resolved(
        K, "r1", "user", decision="allow", option_id="allow", text="ok",
        by="phone"),
    "input.requested": ev.input_requested(
        K, "r2", "which?", kind="choice",
        options=[{"id": "a", "label": "A"}]),
    "plan.updated": ev.plan_updated(
        K, [{"text": "do", "status": "pending"}], text="the plan"),
    "task.updated": ev.task_updated(K, "s1", "subagent", "running",
                                    parent_turn_id="t1", title="look",
                                    can_stop=True),
    "checkpoint.created": ev.checkpoint_created(K, "cp1", files=2,
                                                note="before"),
    "checkpoint.restored": ev.checkpoint_restored(K, "cp1", files=2,
                                                  by="phone"),
    "changes.updated": ev.changes_updated(K, [
        {"path": "a.py", "kind": "add", "added": 1, "removed": 0}]),
    "queue.updated": ev.queue_updated(K, [
        {"command_id": "q1", "text": "next", "by": "phone",
         "mode": "queue", "attachments": ["/tmp/a.png"]}]),
    "command.receipt": ev.command_receipt(K, "c9", "accepted", op="send"),
    "command.result": ev.command_result(K, "c9", "send", True, note="n",
                                        request_id="r1"),
    "usage.updated": ev.usage_updated(K, input_tokens=1, cost_usd=0.5),
    "context.compacted": ev.context_compacted(K, summary="s",
                                              tokens_before=9),
    "error": ev.error(K, "agent_error", "boom", fatal=True),
    "notify": ev.notify(K, "finished", "done", status="done"),
}


class TestSchema(unittest.TestCase):
    """``schema/session_v2.json`` and ``adapters/events.py`` say the
    same thing, so the TypeScript made from one fits what the other
    emits."""

    def setUp(self) -> None:
        self.schema = schema.load()
        self.defs = self.schema["$defs"]

    def test_the_committed_file_is_what_events_py_makes(self):
        # A field added to events.py changes build(); the fix is
        # `python3 -m swe.remote.schema --write`.
        self.assertEqual(self.schema, schema.build())
        self.assertEqual(schema.PATH.read_text(encoding="utf-8"),
                         schema.dumps())
        self.assertEqual(schema.main(["--check"]), 0)

    def test_every_event_type_and_its_fields(self):
        self.assertEqual(set(SAMPLES), set(ev.EVENT_TYPES))
        listed = {ref["$ref"].rsplit("/", 1)[1]
                  for ref in self.defs["Event"]["oneOf"]}
        self.assertEqual(listed, {schema.type_name(t) + "Event"
                                  for t in ev.EVENT_TYPES})
        for etype, spec in ev.SCHEMAS.items():
            name = schema.type_name(etype)
            envelope = self.defs[f"{name}Event"]
            self.assertEqual(envelope["properties"]["type"],
                             {"const": etype})
            self.assertEqual("seq" in envelope["required"],
                             etype in ev.DURABLE_TYPES, etype)
            self.assertEqual("turn_id" in envelope["required"],
                             etype in ev._NEEDS_TURN, etype)
            self.assertEqual("command_id" in envelope["required"],
                             etype in ev._NEEDS_COMMAND, etype)
            data = self.defs[f"{name}Data"]
            extra = set(schema.WIRE_OPTIONAL.get(etype, ()))
            self.assertEqual(
                set(data["properties"]),
                set(spec.required) | set(spec.optional) | extra, etype)
            self.assertEqual(
                set(data["required"]),
                set(spec.required) | set(schema.WIRE_REQUIRED.get(etype, ())),
                etype)
            self.assertIs(data["additionalProperties"], False)

    def test_every_enum(self):
        for (etype, path), values in ev.ENUMS.items():
            node = self.defs[schema.type_name(etype) + "Data"]
            for part in path.split("."):
                node = node["properties"][part.removesuffix("[]")]
                if part.endswith("[]"):
                    node = node["items"]
                if "$ref" in node:
                    node = self.defs[node["$ref"].rsplit("/", 1)[1]]
            self.assertEqual(node["enum"], sorted(values), (etype, path))

    def test_the_two_validators_agree(self):
        for etype, sample in SAMPLES.items():
            self.assertEqual(ev.validate(sample), [], etype)
            self.assertEqual(schema.validate(sample, "Event"), [], etype)
            self.assertEqual(schema.validate(sample), [], etype)
            # An unknown field, a missing required one, a value outside
            # an enum: both say no.
            extra = dict(sample, data=dict(sample["data"], surprise=1))
            self.assertTrue(ev.validate(extra), etype)
            self.assertTrue(schema.validate(extra), etype)
            for name in ev.SCHEMAS[etype].required:
                missing = dict(sample, data={
                    k: v for k, v in sample["data"].items() if k != name})
                self.assertTrue(ev.validate(missing), (etype, name))
                self.assertTrue(schema.validate(missing), (etype, name))
            wrong_v = dict(sample, v=1)
            self.assertTrue(ev.validate(wrong_v))
            self.assertTrue(schema.validate(wrong_v))
        for (etype, path), _values in ev.ENUMS.items():
            if "." in path or "[]" in path:
                continue
            bad = dict(SAMPLES[etype],
                       data=dict(SAMPLES[etype]["data"], **{path: "nope"}))
            self.assertTrue(ev.validate(bad), (etype, path))
            self.assertTrue(schema.validate(bad), (etype, path))

    def test_what_the_wire_adds(self):
        # A delta on the wire always says where its text starts.
        bare = ev.message_delta(K, "m1", "assistant", "hi", 1)
        self.assertEqual(ev.validate(bare), [])
        self.assertEqual(schema.validate(bare),
                         ["Frame.data.offset is required"])
        self.assertTrue(schema.validate(dict(SAMPLES["message.delta"],
                                             seq=3)))
        # The catch-up's state frame.
        state = dict(SAMPLES["session.state"])
        state["data"] = {"state": "running", "head": 4, "epoch": 0,
                         "pending": ["r1"], "live": [
                             {"id": "m1", "kind": "assistant", "text": "",
                              "turn_id": "t1", "done": True}]}
        self.assertEqual(schema.validate(state), [])
        state["data"]["live"][0]["kind"] = "user"
        self.assertTrue(schema.validate(state))
        # The stream's own frames, and nothing else.
        ping = protocol.sync_frame("ping", K, {"ts": 1.0, "head": 3}, ts=1.0)
        self.assertEqual(schema.validate(ping), [])
        self.assertTrue(schema.validate(dict(ping, seq=0)))
        self.assertTrue(schema.validate(dict(ping, data={"ts": 1.0})))
        row = protocol.sync_frame("session.row", K, {"row": {"key": K,
                                                             "x": 1}})
        self.assertEqual(schema.validate(row), [])
        self.assertTrue(schema.validate(protocol.sync_frame(
            "session.row", K, {"row": {}})))
        self.assertEqual(
            schema.validate({"v": 2, "type": "journal.caught-up",
                             "session": K, "ts": 1.0, "data": {}}),
            ["Frame.type 'journal.caught-up' is not a known type"])
        self.assertEqual(
            {schema.type_name(t) + "Frame"
             for t in protocol.SYNC_FRAME_TYPES},
            {"SessionSnapshotFrame", "SessionRowFrame", "PingFrame"})

    def test_an_optional_field_that_is_null_is_left_out_on_the_wire(self):
        # events.validate takes a null optional field as left out, so a
        # journal may hold one. The schema has no nulls there, and the
        # hub leaves the field out when it serves the event.
        thinking = dict(SAMPLES["thinking.completed"], turn_id=None)
        thinking["data"] = dict(thinking["data"], duration_s=None)
        done = dict(SAMPLES["tool.completed"])
        done["data"] = dict(done["data"], name=None, file_edits=[
            {"path": "a.py", "kind": "add", "diff": None},
            {"path": "b.py", "kind": "delete"}])
        started = dict(SAMPLES["tool.started"])
        started["data"] = dict(started["data"], summary=None,
                               input={"timeout": None})
        for event in (thinking, done, started):
            self.assertEqual(ev.validate(event), [])
            self.assertTrue(schema.validate(event))
            self.assertEqual(schema.validate(sync_mod.served(event)), [])
        self.assertEqual(sync_mod.wire(thinking)["data"],
                         {"id": "m2", "text": "hm"})
        self.assertNotIn("turn_id", sync_mod.wire(thinking))
        self.assertEqual(sync_mod.wire(done)["data"]["file_edits"], [
            {"path": "a.py", "kind": "add"},
            {"path": "b.py", "kind": "delete"}])
        self.assertNotIn("name", sync_mod.wire(done)["data"])
        # A tool's own arguments are not looked into.
        self.assertEqual(sync_mod.wire(started)["data"]["input"],
                         {"timeout": None})
        # Nothing to leave out: the same object, not a copy.
        for sample in SAMPLES.values():
            self.assertIs(sync_mod.wire(sample), sample)
        self.assertIn("duration_s", thinking["data"])

    def test_the_constants_a_client_needs(self):
        self.assertEqual(self.schema["x-swe"], {
            "version": 2, "version_header": "X-SWE-Protocol",
            "ping_s": 10.0, "silence_s": 25.0, "replay_max": 500,
            "snapshot_messages": 200,
            "features": ["sync_v2", "snapshot", "live_resume",
                         "delta_offset", "ping", "command_id",
                         "require_keeper", "command_lookup",
                         "session_options", "session_runtime"]})

    def test_the_validator(self):
        root = {"$defs": {
            "A": {"type": "object", "properties": {
                "n": {"anyOf": [{"type": "integer", "minimum": 1},
                                {"type": "null"}]},
                "flag": {"const": True},
                "tags": {"type": "array", "items": {"$ref": "#/$defs/T"}}},
                "required": ["n"], "additionalProperties": False},
            "T": {"type": "string", "minLength": 1},
            "Bad": {"pattern": "x"}}}
        ok = lambda value: schema.validate(value, "A", root)  # noqa: E731
        self.assertEqual(ok({"n": 2, "flag": True, "tags": ["a"]}), [])
        self.assertEqual(ok({"n": None}), [])
        self.assertEqual(ok({"n": True}), ["A.n must be integer"])
        self.assertEqual(ok({"n": 0}), ["A.n must be at least 1"])
        self.assertEqual(ok({"n": 1, "flag": 1}), ["A.flag must be True"])
        self.assertEqual(ok({"n": 1, "tags": ["", 3]}), [
            "A.tags[0] must not be empty", "A.tags[1] must be string"])
        self.assertEqual(ok({"x": 1}), ["A.n is required",
                                        "A.x is not a known field"])
        self.assertEqual(ok([]), ["A must be object"])
        with self.assertRaises(ValueError):
            schema.validate("x", "Bad", root)


if __name__ == "__main__":
    unittest.main()
