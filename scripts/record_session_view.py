"""Record the session view's reducer fixtures from the hub's own v2 stream.

Run from the repository root::

    PYTHONPATH=src:. python3 scripts/record_session_view.py            # write
    PYTHONPATH=src:. python3 scripts/record_session_view.py --check    # compare

Each scenario starts a real hub over a throwaway home with the session
runtime on and a keeper (on a thread) driving the scripted ``FakeAdapter``
(the harness of ``tests/test_remote_sse_v2.py``), opens ``V2Stream`` the way
a client's connection would, and writes down every frame the stream sends:
nothing in a fixture is hand-written. The fixtures are
``tests/fixtures/session-view/<scenario>.json`` and the React client's
reducer tests (``app/web-session/src/core/``) replay them.

A fixture is::

    {"scenario": ..., "about": ...,
     "connections": [{"request": {"last": ..., "epoch": ...},
                      "frames": [<v2 frame>, ...]}, ...],
     "snapshots": [{"after": [<connection>, <frame>], "body": <Snapshot>}]}

``connections`` are one client's connections in order (a reconnect is the
next one, asking with the cursor and epoch it held). ``snapshots`` are
bodies of ``GET /v1/sessions/{key}/snapshot`` taken right after the named
frame; a test that makes the client lose a delta answers its request with
one.

What varies between runs (the session id, the work folder, the hub's boot
id, ids the hub invents) is replaced by fixed names before a fixture is
written (``normalize``), so ``tests/test_session_view_fixtures.py`` can
record again and hold the committed files equal to what the hub emits now.
Timestamps come from the fake clock, which only moves where a scenario
moves it.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from swe.remote.adapters import fake  # noqa: E402

from tests.test_remote_hub_keeper import OPTIONS  # noqa: E402
from tests.test_remote_sse_v2 import V2Case  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "session-view"

SESSION = "claude:session-1"
WORK = "/work/project"
HOME = "/home/user"

#: scenario name -> fixture, filled by ``Recorder``'s tests.
RECORDED: dict[str, dict] = {}


class Connection:
    """One client connection: the frames its stream sent, in order."""

    def __init__(self, case: "Recorder", key: str, index: int,
                 last: int | None = None, epoch: int | None = None) -> None:
        self.case, self.key, self.index = case, key, index
        self.request = {"last": last, "epoch": epoch}
        self.frames: list[dict] = []
        self.stream = case.open(key, last=last or 0, epoch=epoch)
        self.frames.extend(case.check(f) for f in self.stream.catch_up())
        #: The state frame that ended the catch-up.
        self.state = self.frames[-1]["data"]

    def pump(self) -> list[dict]:
        """Everything the stream has to send now."""
        got = self.case.pump(self.stream)
        self.frames.extend(got)
        return got

    def tick(self, seconds: float) -> list[dict]:
        """Move the ping clock and take what the stream sends."""
        self.case.tick.advance(seconds)
        out = []
        while True:
            frames = self.stream.next(block=False)
            if not frames:
                break
            out.extend(self.case.check(f) for f in frames)
        self.frames.extend(out)
        return out

    def close(self) -> None:
        self.stream.close()

    def view(self) -> dict:
        return {"request": self.request, "frames": self.frames}


class Recorder(V2Case):
    """One test per scenario. Each leaves its fixture in ``RECORDED``."""

    def setUp(self) -> None:
        super().setUp()
        self.connections: list[Connection] = []
        self.snapshots: list[dict] = []

    # -- helpers --------------------------------------------------------

    def connect(self, key: str, last=None, epoch=None) -> Connection:
        conn = Connection(self, key, len(self.connections), last, epoch)
        self.connections.append(conn)
        self.addCleanup(conn.close)
        return conn

    def take_snapshot(self, key: str, conn: Connection) -> dict:
        body = self.snapshot(key)
        self.snapshots.append({"after": [conn.index, len(conn.frames) - 1],
                               "body": body})
        return body

    def settle(self, key: str) -> None:
        """The turn that was playing has ended and the hub has it."""
        self.feed.event("turn.ended")
        self.drain(key)

    def finish(self, name: str, about: str, key: str) -> None:
        # The snapshot the hub holds once everything has been read: what the
        # frames of the last connection must add up to.
        last = self.connections[-1]
        if not any(snap["after"] == [last.index, len(last.frames) - 1]
                   for snap in self.snapshots):
            self.take_snapshot(key, last)
        fixture = {
            "scenario": name, "about": inspect.cleandoc(about),
            "connections": [c.view() for c in self.connections],
            "snapshots": self.snapshots,
        }
        RECORDED[name] = normalize(fixture, key, str(self.work),
                                   str(self.home),
                                   self.srv.state.boot_id)

    def approval_id(self, key: str) -> str:
        pending = self.srv.state.sync.snapshot(key)["pending"]
        self.assertEqual(len(pending), 1)
        return pending[0]["data"]["request_id"]

    # -- scenarios ------------------------------------------------------

    def play_out(self, key: str, adapter, conn: Connection) -> None:
        """Play the open turn one step at a time, taking what the stream
        sends after each."""
        while adapter.step():
            self.clock.advance(1)
            self.drain(key)
            conn.pump()

    def test_default(self):
        """A session watched from its first turn to its second: every kind
        of timeline item the reducer draws, then a snapshot taken at the end
        that holds what the frames added up to."""
        self.plans.append({"autoplay": False, "turns": [
            [fake.think("Let me look at the file first.", duration_s=1.5),
             fake.tool("Read", input={"path": "src/app.py"},
                       output="print('hi')", summary="Read src/app.py"),
             fake.emit("plan.updated", items=[
                 {"text": "Read the file", "status": "completed"},
                 {"text": "Change the greeting", "status": "in_progress"},
                 {"text": "Run the tests", "status": "pending"}],
                 text="Greeting change"),
             fake.task("task-1", "running", title="Explore the repo"),
             fake.tool("Edit", input={"path": "src/app.py"}, output="ok",
                       summary="Edit src/app.py",
                       file_edits=[{"path": "src/app.py", "kind": "modify",
                                    "diff": "@@ -1 +1 @@\n-print('hi')\n"
                                            "+print('hello')\n"}]),
             fake.emit("checkpoint.created", checkpoint_id="ck-1", files=1,
                       note="before the edit"),
             fake.emit("changes.updated", files=[
                 {"path": "src/app.py", "kind": "modify", "added": 1,
                  "removed": 1}]),
             fake.emit("usage.updated", input_tokens=1200,
                       output_tokens=340, cache_read_tokens=800,
                       context_tokens=1540, context_window=200000,
                       cost_usd=0.012),
             fake.emit("tool.started", call_id="call-9", name="Bash",
                       summary="npm test", input={"command": "npm test"}),
             fake.emit("tool.updated", call_id="call-9",
                       text="running 3 tests"),
             fake.emit("tool.completed", call_id="call-9", name="Bash",
                       status="error", output="1 failing"),
             fake.task("task-1", "completed", title="Explore the repo"),
             fake.emit("checkpoint.restored", checkpoint_id="ck-1", files=1,
                       by="the phone"),
             fake.say("Done. I changed the greeting in `src/app.py`.",
                      chunks=["Done. I changed ", "the greeting in ",
                              "`src/app.py`."])],
            [fake.say("Second reply", chunks=["Second ", "reply"])]]})
        key = self.launch()
        adapter = self.adapters[0]
        conn = self.connect(key)
        self.play_out(key, adapter, conn)
        self.drain(key)
        conn.pump()
        status, out = self.post(key, "message",
                                {"text": "once more", "client_id": "m2"})
        self.assertEqual(status, 202, out)
        self.play_out(key, adapter, conn)
        self.drain(key)
        conn.pump()
        self.take_snapshot(key, conn)
        self.finish("default", self.test_default.__doc__, key)

    def test_streaming(self):
        """A reply written in four chunks, watched from the start, with a
        ping and a snapshot taken after the third chunk (what a client that
        lost the second asks for)."""
        key, adapter = self.streaming("Hel", "lo ", "wor", "ld")
        conn = self.connect(key)
        for n in range(4):
            self.clock.advance(1)
            self.say(key, adapter)
            conn.pump()
            if n == 2:
                self.take_snapshot(key, conn)
                conn.tick(10)
        self.say(key, adapter)
        conn.pump()
        self.finish("streaming", self.test_streaming.__doc__, key)

    def test_approval(self):
        """A tool approval and a question, each pending, then answered from
        the same device. A second device connects while the approval is
        open and takes it from its snapshot."""
        key = self.launch(
            [fake.say("I will clean up.", chunks=["I will ", "clean up."]),
             fake.ask("Run `rm -rf build`?", options=OPTIONS),
             fake.tool("Bash", input={"command": "rm -rf build"},
                       output="", summary="rm -rf build"),
             fake.ask_input("Which branch should I push to?"),
             fake.say("Pushed.")])
        asked = self.feed.event("approval.requested")
        self.drain(key)
        first = self.connect(key)
        self.clock.advance(2)
        second = self.connect(key)
        status, out = self.post(key, "approval", {
            "request_id": asked["data"]["request_id"], "decision": "allow"})
        self.assertEqual(status, 200, out)
        asked = self.feed.event("input.requested")
        self.drain(key)
        first.pump()
        second.pump()
        self.clock.advance(3)
        status, out = self.post(key, "approval", {
            "request_id": asked["data"]["request_id"], "decision": "allow"})
        self.assertEqual(status, 200, out)
        self.settle(key)
        first.pump()
        second.pump()
        self.finish("approval", self.test_approval.__doc__, key)

    def test_queued(self):
        """A message sent while the agent waits on an approval is queued,
        then sent when the turn ends."""
        key = self.launch(
            [fake.ask("Run the tests?", options=OPTIONS)],
            [fake.say("Second turn.", chunks=["Second ", "turn."])])
        asked = self.feed.event("approval.requested")
        conn = self.connect(key)
        status, out = self.post(key, "message",
                                {"text": "then lint it", "client_id": "q1"})
        self.assertEqual(status, 202, out)
        self.feed.event("queue.updated")
        self.drain(key)
        conn.pump()
        self.clock.advance(4)
        status, out = self.post(key, "approval", {
            "request_id": asked["data"]["request_id"], "decision": "allow"})
        self.assertEqual(status, 200, out)
        self.feed.result("q1")
        self.drain(key)
        conn.pump()
        self.finish("queued", self.test_queued.__doc__, key)

    def test_steer(self):
        """A queued message is sent now: it leaves the queue and joins the
        turn in flight."""
        key = self.launch([fake.ask("Run the tests?", options=OPTIONS)])
        self.feed.event("approval.requested")
        conn = self.connect(key)
        for cid in ("q1", "q2"):
            status, out = self.post(key, "message", {
                "text": f"text {cid}", "client_id": cid})
            self.assertEqual(status, 202, out)
        self.feed.until(lambda e: e["type"] == "queue.updated"
                        and len(e["data"]["items"]) == 2, "two queued")
        self.drain(key)
        conn.pump()
        self.clock.advance(2)
        status, out = self.post(key, "queue", {"client_id": "q2",
                                               "op": "steer"})
        self.assertEqual(status, 200, out)
        self.feed.until(lambda e: e["type"] == "message.completed"
                        and e["data"]["text"] == "text q2", "the steer")
        self.drain(key)
        conn.pump()
        status, out = self.post(key, "queue", {"client_id": "q1",
                                               "op": "cancel"})
        self.assertEqual(status, 200, out)
        self.feed.result("q1")
        self.drain(key)
        conn.pump()
        self.finish("steer", self.test_steer.__doc__, key)

    def test_failed_turn(self):
        """A turn that fails (an error, then turn.ended failed) and the
        context being compacted."""
        key = self.launch(
            [fake.say("Working.", chunks=["Work", "ing."]),
             fake.emit("context.compacted", summary="Earlier work, shortened.",
                       tokens_before=90000),
             fake.fail("The agent could not continue.", code="agent_error")])
        self.drain(key)
        conn = self.connect(key)
        self.take_snapshot(key, conn)
        self.finish("failed_turn", self.test_failed_turn.__doc__, key)

    def test_reconnect(self):
        """A client is cut off in the middle of a reply and comes back with
        its cursor and epoch: the events it missed are replayed, then the
        row and the state, whose live[] holds the text so far; the next
        delta starts where that text ends."""
        key, adapter = self.streaming("Hel", "lo ", "wor", "ld",
                                      after=[fake.tool("Bash", output="x")])
        first = self.connect(key)
        self.say(key, adapter)
        first.pump()
        cursor, epoch = first.state["head"], first.state["epoch"]
        first.close()
        # Away: two more chunks arrive and nobody reads them.
        self.clock.advance(3)
        self.say(key, adapter, 2)
        self.clock.advance(8)
        second = self.connect(key, last=cursor, epoch=epoch)
        self.say(key, adapter)
        second.pump()
        self.say(key, adapter, 2)
        second.pump()
        self.finish("reconnect", self.test_reconnect.__doc__, key)

    def test_overflow(self):
        """A stream that is not read while the agent runs a reply, six
        tools and the start of the next reply: its queue overflows, and it
        is sent the events from its last seq, then the row and the state
        with the text in progress."""
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
        self.srv.state.sync.queue_max = 4
        conn = self.connect(key)
        self.say(key, adapter, 3 + 6 + 1)
        self.assertTrue(conn.stream.overflow)
        conn.pump()
        self.say(key, adapter, 2)
        conn.pump()
        self.finish("overflow", self.test_overflow.__doc__, key)

    def test_epoch_change(self):
        """The hub's journal starts over while a client is connected: it is
        sent a snapshot on the new epoch. A client that reconnects with the
        old epoch gets one too."""
        key = self.launch([fake.say("one", chunks=["o", "ne"])],
                          [fake.say("two", chunks=["t", "wo"])])
        self.settle(key)
        conn = self.connect(key)
        cursor, epoch = conn.state["head"], conn.state["epoch"]
        self.assertEqual(self.srv.state.sync.new_epoch(key), epoch + 1)
        conn.pump()
        self.clock.advance(5)
        status, out = self.post(key, "message",
                                {"text": "more", "client_id": "m2"})
        self.assertEqual(status, 202, out)
        self.settle(key)
        conn.pump()
        conn.close()
        self.connect(key, last=cursor, epoch=epoch)
        self.finish("epoch_change", self.test_epoch_change.__doc__, key)


# ---------------------------------------------------------------------


_COMMAND_ID = re.compile(r"\b[hs][0-9a-f]{24}\b")
_DEVICE_ID = re.compile(r"[0-9a-f]{16}")
#: Anything at or past this is a wall-clock time, not the fake clock's.
_WALL_CLOCK = 1.77e9
_FIXED_WALL_TIME = 1760000100.0
_LAUNCH_PROMPT = re.compile(r"\b[a-z0-9]+-prompt\b")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalize(fixture: dict, key: str, work: str, home: str,
              boot_id: str) -> dict:
    """The fixture with everything that differs between runs replaced by
    a fixed name. Names are numbered in order of first appearance."""
    text = json.dumps(fixture, sort_keys=False)
    uuid = key.split(":", 1)[1]
    text = text.replace(work, WORK).replace(home, HOME)
    text = text.replace(key, SESSION).replace(uuid, "uuid-1")
    text = text.replace(boot_id, "boot-1")
    for pattern, stem in ((_COMMAND_ID, "hub-command-"),
                          (_LAUNCH_PROMPT, "launch-prompt-")):
        names: dict[str, str] = {}
        for found in pattern.findall(text):
            names.setdefault(found, f"{stem}{len(names) + 1}")
        for old, new in names.items():
            text = text.replace(old, new)
    fixture = json.loads(text)
    devices: dict[str, str] = {}

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for field, value in node.items():
                if field in ("last_active_at", "ts") \
                        and isinstance(value, float) \
                        and value >= _WALL_CLOCK:
                    node[field] = _FIXED_WALL_TIME
                elif field == "machine_id" and isinstance(value, str):
                    node[field] = "machine-1"
                elif field == "by" and isinstance(value, str) \
                        and _DEVICE_ID.fullmatch(value):
                    node[field] = devices.setdefault(
                        value, f"device-{len(devices) + 1}")
                else:
                    walk(value)

    walk(fixture)
    return fixture


def record_all(stream=None) -> dict[str, dict]:
    """Run every scenario and return ``{name: fixture}``. ``stream`` takes
    the runner's report (stderr by default)."""
    RECORDED.clear()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Recorder)
    result = unittest.TextTestRunner(stream=stream or sys.stderr,
                                     verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("a session-view scenario failed")
    return dict(RECORDED)


def render(fixture: dict) -> str:
    """The text a fixture file holds: one frame per line, so a change to
    the hub's output reads as a small diff."""
    out = ["{",
           f'  "scenario": {json.dumps(fixture["scenario"])},',
           f'  "about": {json.dumps(fixture["about"])},',
           '  "connections": [']
    conns = []
    for conn in fixture["connections"]:
        frames = ",\n".join("      " + json.dumps(f, separators=(",", ":"))
                            for f in conn["frames"])
        request = json.dumps(conn["request"], separators=(",", ":"))
        conns.append(f'    {{"request": {request}, "frames": [\n{frames}\n'
                     "    ]}")
    out.append(",\n".join(conns))
    out.append("  ],")
    snaps = []
    for snap in fixture["snapshots"]:
        after = json.dumps(snap["after"], separators=(",", ":"))
        body = json.dumps(snap["body"], separators=(",", ":"))
        snaps.append(f'    {{"after": {after}, "body": {body}}}')
    out.append('  "snapshots": [' + ("\n" + ",\n".join(snaps) + "\n  "
                                       if snaps else "") + "]")
    out.append("}")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    try:
        recorded = record_all()
    except RuntimeError as exc:
        print(f"{exc}; nothing was written", file=sys.stderr)
        return 1
    stale = []
    for name, fixture in sorted(recorded.items()):
        path = FIXTURES / f"{name}.json"
        text = render(fixture)
        if "--check" in argv:
            if not path.exists() or path.read_text() != text:
                stale.append(path.name)
        else:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if stale:
        print("stale fixtures: " + ", ".join(stale)
              + " (run scripts/record_session_view.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
