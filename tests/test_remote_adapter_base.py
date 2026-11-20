"""The SessionAdapter contract (remote/adapters/base.py) and the scripted
fake adapter (remote/adapters/fake.py).

``AdapterContract`` is the reusable conformance suite: a later adapter's
tests mix it into a ``unittest.TestCase`` and implement
``make_adapter``::

    from tests.test_remote_adapter_base import AdapterContract

    class TestCodexContract(AdapterContract, unittest.TestCase):
        def make_adapter(self, *, caps, reply):
            ...  # an adapter over a fake agent that answers ``reply``
"""

import unittest

from swe.remote.adapters import base, events, fake
from swe.remote.adapters.base import (
    AdapterError, Answer, Caps, SessionAdapter, StartSpec, TurnInput,
    UnknownRequest, Unsupported,
)
from swe.remote.adapters.registry import ADAPTER_ID_RE
from tests._tempdir import temp_dir

ALL_CAPS = Caps(steer=True, plan_mode=True, rollback=True, fork=True,
                stop_task=True, options={"model", "effort",
                                         "permission_mode"})


class AdapterContract:
    """What every SessionAdapter must do. Mix into a TestCase and give
    ``make_adapter(caps=, reply=)``: a fresh, unstarted adapter whose
    caps are ``caps`` and whose next turn answers ``reply``."""

    TIMEOUT_S = 5.0

    def make_adapter(self, *, caps: Caps, reply: str) -> SessionAdapter:
        raise NotImplementedError

    def make_spec(self, **kw) -> StartSpec:
        return StartSpec(session_key="contract:1",
                         cwd=str(temp_dir(self)), **kw)

    def started(self, caps: Caps = Caps(), reply: str = "hello"):
        adapter = self.make_adapter(caps=caps, reply=reply)
        sink = fake.ListSink()
        sid = adapter.start(self.make_spec(), sink)
        self.addCleanup(adapter.close, 0.5)
        return adapter, sink, sid

    def wait_turn_end(self, sink, turn_id):
        ended = sink.wait_for(lambda e: e["type"] == "turn.ended"
                              and e.get("turn_id") == turn_id,
                              self.TIMEOUT_S)
        self.assertIsNotNone(ended, "the turn never ended")
        return ended

    def test_contract_structure(self):
        adapter = self.make_adapter(caps=Caps(), reply="x")
        self.assertEqual(base.missing_methods(adapter), [])
        self.assertIsInstance(adapter, SessionAdapter)
        self.assertTrue(ADAPTER_ID_RE.fullmatch(adapter.adapter_id))
        self.assertIsInstance(adapter.capabilities, Caps)

    def test_contract_calls_before_start_raise(self):
        adapter = self.make_adapter(caps=Caps(), reply="x")
        with self.assertRaises(AdapterError):
            adapter.send_turn(TurnInput("hi"))

    def test_contract_start_emits_caps_then_state(self):
        _adapter, sink, sid = self.started()
        self.assertIsInstance(sid, str)
        self.assertTrue(sid)
        self.assertEqual(sink.types()[:2], ["session.caps", "session.state"])
        for event in sink.events:
            self.assertEqual(events.validate(event), [])
            self.assertEqual(event["seq"], 0, "the sink stamps seq")

    def test_contract_turn_order(self):
        adapter, sink, _sid = self.started(reply="hello there")
        turn_id = adapter.send_turn(TurnInput("hi", command_id="c1",
                                              device="phone"))
        self.assertTrue(turn_id)
        self.wait_turn_end(sink, turn_id)
        mine = [e for e in sink.events if e.get("turn_id") == turn_id]
        self.assertEqual(mine[0]["type"], "turn.started")
        self.assertEqual(mine[-1]["type"], "turn.ended")
        self.assertEqual(mine[0].get("command_id"), "c1")
        replies = [e["data"]["text"] for e in mine
                   if e["type"] == "message.completed"
                   and e["data"]["kind"] == "assistant"]
        self.assertIn("hello there", replies)
        # Every delta precedes its message's completion.
        done_ids: set = set()
        for event in mine:
            if event["type"] == "message.completed":
                done_ids.add(event["data"]["id"])
            if event["type"] == "message.delta":
                self.assertNotIn(event["data"]["id"], done_ids)

    def test_contract_optional_methods_gated_by_caps(self):
        adapter, _sink, _sid = self.started(caps=Caps())
        turn = TurnInput("more")
        for call in (lambda: adapter.steer(turn),
                     lambda: adapter.stop_task("task-1"),
                     lambda: adapter.rollback("t1"),
                     lambda: adapter.set_option("plan_mode", True),
                     lambda: adapter.set_option("model", "m-1")):
            with self.assertRaises(Unsupported):
                call()
        forker = self.make_adapter(caps=Caps(), reply="x")
        with self.assertRaises(Unsupported) as ctx:
            forker.start(self.make_spec(resume_id="s-1", fork=True),
                         fake.ListSink())
        self.assertEqual(ctx.exception.feature, "fork")

    def test_contract_steer_needs_a_turn(self):
        adapter, sink, _sid = self.started(caps=ALL_CAPS)
        if not adapter.capabilities.steer:
            self.skipTest("this adapter offers no steer")
        before = len(sink.events)
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("more", attachments=["/w/a.png"]))
        self.assertEqual(ctx.exception.code, "idle")
        self.assertEqual(len(sink.events), before, "a refused steer emits "
                         "nothing")

    def test_contract_respond_unknown_request(self):
        adapter, _sink, _sid = self.started()
        with self.assertRaises(UnknownRequest):
            adapter.respond("nope", Answer(decision="allow"))

    def test_contract_interrupt_when_idle_is_quiet(self):
        adapter, sink, _sid = self.started()
        before = len(sink.events)
        adapter.interrupt()
        self.assertEqual(len(sink.events), before)

    def test_contract_close_ends_the_session(self):
        adapter, sink, _sid = self.started()
        adapter.close(0.5)
        self.assertEqual(sink.events[-1]["type"], "session.state")
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")
        count = len(sink.events)
        adapter.close(0.5)
        self.assertEqual(len(sink.events), count, "close is idempotent")
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("hi"))
        self.assertEqual(ctx.exception.code, "agent_down")

    def test_contract_start_after_close_raises(self):
        adapter = self.make_adapter(caps=Caps(), reply="x")
        adapter.close(0.5)
        with self.assertRaises(AdapterError):
            adapter.start(self.make_spec(), fake.ListSink())
        started, sink, _sid = self.started()
        started.close(0.5)
        count = len(sink.events)
        with self.assertRaises(AdapterError):
            started.start(self.make_spec(), fake.ListSink())
        self.assertEqual(len(sink.events), count)


class TestFakeContract(AdapterContract, unittest.TestCase):
    def make_adapter(self, *, caps, reply):
        return fake.FakeAdapter(caps=caps, turns=[[fake.say(reply)]])


class TestCaps(unittest.TestCase):
    def test_defaults_are_off(self):
        caps = Caps()
        for feature in base.FEATURES:
            self.assertFalse(caps.supports(feature))
        self.assertFalse(caps.can_set("model"))
        self.assertFalse(caps.can_set("plan_mode"))

    def test_require_and_can_set(self):
        caps = Caps(steer=True, plan_mode=True, options=["model"])
        caps.require("steer")
        with self.assertRaises(Unsupported) as ctx:
            caps.require("rollback")
        self.assertEqual(ctx.exception.feature, "rollback")
        self.assertEqual(ctx.exception.code, "unsupported")
        self.assertTrue(caps.can_set("plan_mode"))
        self.assertTrue(caps.can_set("model"))
        self.assertFalse(caps.can_set("effort"))
        self.assertIsInstance(caps.options, frozenset)

    def test_unknown_names_are_refused(self):
        with self.assertRaises(ValueError):
            Caps(options={"plan_mode"})
        with self.assertRaises(ValueError):
            Caps(options={"colour"})
        with self.assertRaises(ValueError):
            Caps().supports("teleport")

    def test_narrowed_and_as_dict(self):
        ceiling = Caps(steer=True, rollback=True, options={"model", "effort"})
        told = Caps(steer=True, plan_mode=True, options={"model"})
        both = ceiling.narrowed(told)
        self.assertEqual(both, Caps(steer=True, options={"model"}))
        self.assertEqual(both.as_dict(), {
            "steer": True, "plan_mode": False, "rollback": False,
            "fork": False, "stop_task": False, "options": ["model"]})
        # session.caps carries exactly that shape.
        events.session_caps("k", ALL_CAPS.as_dict())


class TestDataclasses(unittest.TestCase):
    def test_start_spec_checks(self):
        spec = StartSpec("k", "/w", argv=["a", "b"])
        self.assertEqual(spec.argv, ("a", "b"))
        for bad in ({"session_key": "", "cwd": "/w"},
                    {"session_key": "k", "cwd": "rel"},
                    {"session_key": "k", "cwd": "/w", "fork": True},
                    {"session_key": "k", "cwd": "/w", "argv": []},
                    {"session_key": "k", "cwd": "/w", "argv": ["a", ""]}):
            with self.assertRaises(ValueError, msg=bad):
                StartSpec(**bad)

    def test_turn_input_and_answer(self):
        turn = TurnInput("hi", attachments=["/a.png"], command_id="c",
                         device="d")
        self.assertEqual(turn.attachments, ("/a.png",))
        for text in ("", "  ", None):
            with self.assertRaises(ValueError):
                TurnInput(text)
        Answer(option_id="o1")
        Answer(text="yes")
        with self.assertRaises(ValueError):
            Answer()
        with self.assertRaises(ValueError):
            Answer(decision="maybe")

    def test_errors_carry_codes(self):
        err = AdapterError("boom", code="timeout")
        self.assertEqual((str(err), err.code), ("boom", "timeout"))
        self.assertEqual(AdapterError("x").code, "adapter_error")
        unknown = UnknownRequest("r9")
        self.assertIsInstance(unknown, AdapterError)
        self.assertEqual(unknown.request_id, "r9")
        self.assertIn("r9", str(unknown))
        self.assertIn("steer", str(Unsupported("steer")))
        self.assertEqual(str(Unsupported("steer", "not here")), "not here")

    def test_check_option(self):
        caps = Caps(plan_mode=True, options={"model"})
        base.check_option(caps, "plan_mode", True)
        base.check_option(caps, "model", "opus")
        with self.assertRaises(ValueError):
            base.check_option(caps, "colour", "red")
        with self.assertRaises(ValueError):
            base.check_option(caps, "plan_mode", "yes")
        with self.assertRaises(ValueError):
            base.check_option(caps, "model", "two words")
        with self.assertRaises(ValueError):
            base.check_option(caps, "model", "")
        with self.assertRaises(Unsupported):
            base.check_option(caps, "effort", "high")

    def test_missing_methods(self):
        class Half:
            adapter_id = "half"

            def start(self, spec, sink):
                return "x"

        missing = base.missing_methods(Half())
        self.assertIn("send_turn", missing)
        self.assertIn("capabilities", missing)
        self.assertNotIn("start", missing)

        class Broken(fake.FakeAdapter):
            adapter_id = 3

            @property
            def capabilities(self):
                raise RuntimeError("no")

        self.assertEqual(base.missing_methods(Broken()),
                         ["adapter_id", "capabilities"])


class TestFakeAdapter(unittest.TestCase):
    def start(self, turns=(), *, caps=ALL_CAPS, **kw):
        clock = fake.FakeClock(100.0)
        adapter = fake.FakeAdapter(turns=turns, caps=caps, clock=clock, **kw)
        sink = fake.ListSink()
        adapter.start(StartSpec("fake:1", "/w"), sink)
        return adapter, sink, clock

    def test_clock(self):
        clock = fake.FakeClock(5)
        self.assertEqual(clock(), 5.0)
        self.assertEqual(clock.advance(2.5), 7.5)
        with self.assertRaises(ValueError):
            clock.advance(-1)

    def test_a_whole_turn_with_an_approval(self):
        adapter, sink, clock = self.start([[
            fake.think("plan it", duration_s=2.0),
            fake.tool("Edit", input={"file_path": "/w/a.py"}, output="ok",
                      summary="Edit · /w/a.py",
                      file_edits=[{"path": "/w/a.py", "kind": "modify",
                                   "diff": "@@ -1 +1 @@\n-a\n+b\n"}],
                      after=1.0),
            fake.ask("Run tests?", kind="command",
                     options=[{"id": "yes", "label": "Yes"},
                              {"id": "no", "label": "No"}]),
            fake.say("All green.", chunks=["All ", "green."], after=3.0),
        ]])
        turn_id = adapter.send_turn(TurnInput("fix it", device="phone",
                                              command_id="c1",
                                              attachments=["/a.png"]))
        self.assertEqual(turn_id, "t1")
        self.assertEqual(sink.types()[-2:],
                         ["approval.requested", "session.state"])
        self.assertEqual(sink.events[-1]["data"]["state"], "waiting")
        self.assertEqual(adapter.pending, {"r1": "approval"})
        self.assertEqual(clock(), 101.0)
        user = sink.of("message.completed")[0]
        self.assertEqual(user["data"], {"id": "m1", "kind": "user",
                                        "text": "fix it", "by": "phone",
                                        "attachments": ["/a.png"]})
        self.assertEqual(user["command_id"], "c1")

        adapter.respond("r1", Answer(option_id="yes", device="mac"))
        resolved = sink.of("approval.resolved")[0]
        self.assertEqual(resolved["data"], {"request_id": "r1",
                                            "source": "user",
                                            "option_id": "yes", "by": "mac"})
        deltas = sink.of("message.delta")
        self.assertEqual([(d["data"]["seq"], d["data"]["offset"],
                           d["data"]["done"]) for d in deltas],
                         [(1, 0, False), (2, 4, True)])
        ended = sink.of("turn.ended")[0]
        self.assertEqual(ended["data"], {"status": "completed",
                                         "duration_s": 4.0})
        self.assertEqual(ended["command_id"], "c1")
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        self.assertEqual(adapter.calls[0].name, "start")
        self.assertEqual([c.name for c in adapter.calls[1:]],
                         ["send_turn", "respond"])

    def test_resume_fork_and_start_options(self):
        adapter = fake.FakeAdapter(caps=ALL_CAPS)
        sid = adapter.start(StartSpec("k", "/w", resume_id="abc",
                                      model="opus", plan_mode=True),
                            fake.ListSink())
        self.assertEqual(sid, "abc")
        self.assertEqual(adapter.options, {"model": "opus",
                                           "plan_mode": True})
        with self.assertRaises(AdapterError):
            adapter.start(StartSpec("k", "/w"), fake.ListSink())
        forked = fake.FakeAdapter(caps=ALL_CAPS)
        sink = fake.ListSink()
        self.assertEqual(forked.start(StartSpec("k", "/w", resume_id="abc",
                                                fork=True), sink),
                         "abc-fork")
        self.assertEqual(sink.of("session.state")[0]["data"],
                         {"state": "idle", "harness_session_id": "abc-fork"})
        # caps from a registry resolution when none are given.
        resolved = type("R", (), {"caps": Caps(steer=True)})()
        self.assertTrue(fake.FakeAdapter(resolved).capabilities.steer)

    def test_busy_and_idle_errors(self):
        adapter, _sink, _clock = self.start([[fake.ask("ok?")]])
        adapter.send_turn(TurnInput("go"))
        with self.assertRaises(AdapterError) as ctx:
            adapter.send_turn(TurnInput("again"))
        self.assertEqual(ctx.exception.code, "busy")
        with self.assertRaises(AdapterError):
            adapter.rollback("t1")
        adapter.interrupt()
        with self.assertRaises(AdapterError) as ctx:
            adapter.steer(TurnInput("late"))
        self.assertEqual(ctx.exception.code, "idle")

    def test_interrupt_retires_the_open_approval(self):
        adapter, sink, _clock = self.start([[fake.ask("ok?"),
                                             fake.say("never")]])
        adapter.send_turn(TurnInput("go"))
        adapter.interrupt()
        self.assertEqual(sink.types()[-3:], ["approval.resolved",
                                             "turn.ended", "session.state"])
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["source"],
                         "turn_ended")
        self.assertEqual(sink.of("turn.ended")[0]["data"]["status"],
                         "interrupted")
        self.assertEqual(adapter.pending, {})
        with self.assertRaises(UnknownRequest):
            adapter.respond("r1", Answer(decision="allow"))
        self.assertNotIn("never", [e["data"].get("text")
                                   for e in sink.of("message.completed")])

    def test_fail_mid_turn(self):
        adapter, sink, _clock = self.start([
            [fake.say("part"), fake.fail("rate limited", code="limit"),
             fake.say("unreached")],
            [fake.say("second")],
        ])
        adapter.send_turn(TurnInput("go"))
        err = sink.of("error")[0]
        self.assertEqual(err["data"], {"code": "limit",
                                       "message": "rate limited"})
        self.assertEqual(sink.of("turn.ended")[0]["data"]["status"], "failed")
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")
        # The next turn plays the next script.
        adapter.send_turn(TurnInput("again"))
        texts = [e["data"]["text"] for e in sink.of("message.completed")]
        self.assertEqual(texts, ["go", "part", "again", "second"])

    def test_agent_exit_follows_the_contract(self):
        adapter, sink, _clock = self.start([[fake.ask("ok?")]])
        adapter.send_turn(TurnInput("go"))
        sink.clear()
        adapter.crash("killed")
        self.assertEqual(sink.types(), ["approval.resolved", "turn.ended",
                                        "session.state"])
        self.assertEqual(sink.events[0]["data"]["source"], "agent_exit")
        self.assertEqual(sink.events[1]["data"]["status"], "failed")
        self.assertEqual(sink.events[2]["data"],
                         {"state": "agent_down", "reason": "killed"})
        adapter.crash()
        adapter.close()
        self.assertEqual(len(sink.events), 3, "nothing after the exit")
        for call in (lambda: adapter.send_turn(TurnInput("x")),
                     lambda: adapter.interrupt(),
                     lambda: adapter.respond("r1", Answer(text="x"))):
            with self.assertRaises(AdapterError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "agent_down")

    def test_scripted_exit_step(self):
        adapter, sink, _clock = self.start([[fake.say("bye"),
                                             fake.exit_agent()]])
        adapter.send_turn(TurnInput("go"))
        self.assertEqual(sink.events[-1]["data"]["state"], "agent_down")
        self.assertFalse(adapter.alive)

    def test_close_mid_turn(self):
        adapter, sink, _clock = self.start([[fake.ask_input("Name?")]])
        adapter.send_turn(TurnInput("go"))
        self.assertEqual(sink.of("input.requested")[0]["data"],
                         {"request_id": "r1", "prompt": "Name?",
                          "kind": "text"})
        adapter.close(0.1)
        self.assertEqual(sink.types()[-3:], ["approval.resolved",
                                             "turn.ended", "session.state"])
        self.assertEqual(sink.events[-3]["data"]["source"], "closed")
        self.assertEqual(sink.events[-1]["data"]["state"], "closed")

    def test_close_before_start(self):
        adapter = fake.FakeAdapter()
        adapter.close()
        adapter.close()
        self.assertEqual([c.name for c in adapter.calls], ["close", "close"])
        with self.assertRaises(AdapterError) as ctx:
            adapter.start(StartSpec("k", "/w"), fake.ListSink())
        self.assertEqual(ctx.exception.code, "closed")
        self.assertIsNone(adapter.spec)
        self.assertFalse(adapter.alive)

    def test_input_answer_and_manual_stepping(self):
        adapter, sink, _clock = self.start(
            [[fake.ask_input("Name?", request_id="q1"), fake.wait(5),
              fake.say("Hi Ada", stream=False)]], autoplay=False)
        adapter.send_turn(TurnInput("go"))
        self.assertEqual(sink.types()[-1], "message.completed")
        self.assertTrue(adapter.step())
        self.assertFalse(adapter.step(), "blocked on the question")
        adapter.respond("q1", Answer(text="Ada"))
        self.assertEqual(sink.types()[-1], "session.state")
        adapter.play()
        self.assertEqual(sink.of("approval.resolved")[0]["data"]["text"],
                         "Ada")
        self.assertEqual(sink.of("message.delta"), [])
        self.assertEqual(sink.of("turn.ended")[0]["data"]["duration_s"], 5.0)
        self.assertFalse(adapter.step(), "no turn")

    def test_steer_options_tasks_rollback(self):
        adapter, sink, _clock = self.start([
            [fake.task("sub-1", title="Explore", after=0.5),
             fake.ask("ok?")],
            [fake.emit("plan.updated",
                       items=[{"text": "a", "status": "pending"}])],
            [fake.emit("command.result", command_id="c9", op="message",
                       ok=True)],
        ])
        adapter.send_turn(TurnInput("go"))
        adapter.steer(TurnInput("also this", device="mac"))
        self.assertEqual(sink.of("message.completed")[-1]["turn_id"], "t1")
        self.assertNotIn("attachments", sink.of("message.completed")[-1]["data"])
        adapter.steer(TurnInput("and these", device="mac",
                                attachments=["/w/a.png", "/w/b.pdf"]))
        steered = sink.of("message.completed")[-1]
        self.assertEqual(steered["data"]["attachments"],
                         ["/w/a.png", "/w/b.pdf"])
        self.assertEqual(steered["data"]["by"], "mac")
        adapter.stop_task("sub-1")
        stopped = sink.of("task.updated")[-1]["data"]
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["parent_turn_id"], "t1")
        with self.assertRaises(AdapterError) as ctx:
            adapter.stop_task("ghost")
        self.assertEqual(ctx.exception.code, "unknown_task")
        adapter.respond("r1", Answer(decision="allow"))
        adapter.set_option("model", "opus")
        adapter.set_option("plan_mode", True)
        self.assertEqual(adapter.options, {"model": "opus",
                                           "plan_mode": True})
        adapter.send_turn(TurnInput("t2"))
        self.assertEqual(len(sink.of("plan.updated")), 1)
        adapter.send_turn(TurnInput("t3"))
        self.assertEqual(sink.of("command.result")[0]["command_id"], "c9")
        self.assertEqual(adapter.finished_turns, ["t1", "t2", "t3"])
        adapter.rollback("t2")
        self.assertEqual(adapter.finished_turns, ["t1"])
        with self.assertRaises(AdapterError) as ctx:
            adapter.rollback("t9")
        self.assertEqual(ctx.exception.code, "unknown_turn")
        # A turn past the script just ends.
        adapter.send_turn(TurnInput("t4"))
        self.assertEqual(sink.events[-1]["data"]["state"], "idle")

    def test_transcript_position(self):
        pos = base.TranscriptPosition("/x.jsonl", 10)
        adapter = fake.FakeAdapter(position=pos)
        self.assertIs(adapter.transcript_position(), pos)
        self.assertEqual(adapter.calls[-1].name, "transcript_position")


class TestListSink(unittest.TestCase):
    def test_rejects_bad_events(self):
        sink = fake.ListSink()
        with self.assertRaises(AssertionError):
            sink.emit({"v": 2, "type": "nope"})
        loose = fake.ListSink(check=False)
        loose.emit({"type": "anything"})
        self.assertEqual(loose.types(), ["anything"])

    def test_wait_for(self):
        import threading

        sink = fake.ListSink()
        state = events.session_state("k", "idle")
        timer = threading.Timer(0.01, sink.emit, args=(state,))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(sink.wait_for("session.state", 5.0), state)
        self.assertIsNone(sink.wait_for("turn.ended", 0.01))


if __name__ == "__main__":
    unittest.main()
