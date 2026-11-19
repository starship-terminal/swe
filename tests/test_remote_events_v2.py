"""The canonical v2 event model (remote/adapters/events.py)."""

import copy
import unittest

from swe.remote import protocol
from swe.remote.adapters import events as ev

S = "claude:abc"

# One valid event of every type, through its builder.
SAMPLES = {
    "session.state": lambda: ev.session_state(S, "running", reason="go",
                                              harness_session_id="abc"),
    "session.caps": lambda: ev.session_caps(S, {
        "steer": False, "plan_mode": True, "rollback": False,
        "fork": False, "stop_task": True, "options": ["model"]}),
    "turn.started": lambda: ev.turn_started(S, "t1", by="phone", text="hi",
                                            command_id="c1"),
    "turn.ended": lambda: ev.turn_ended(S, "t1", "completed",
                                        duration_s=149.2, command_id="c1"),
    "message.delta": lambda: ev.message_delta(S, "m1", "assistant", "Hel",
                                              1, offset=0, turn_id="t1"),
    "message.completed": lambda: ev.message_completed(
        S, "m1", "assistant", "Hello", turn_id="t1"),
    "thinking.completed": lambda: ev.thinking_completed(
        S, "m2", "pondering", duration_s=3.5, turn_id="t1"),
    "tool.started": lambda: ev.tool_started(
        S, "call-1", "Bash", summary="Bash · ls", input={"command": "ls"},
        turn_id="t1"),
    "tool.updated": lambda: ev.tool_updated(S, "call-1", text="line 1\n"),
    "tool.completed": lambda: ev.tool_completed(
        S, "call-1", "ok", name="Edit", output="done",
        file_edits=[{"path": "/w/a.py", "kind": "modify",
                     "diff": "@@ -1 +1 @@\n-a\n+b\n"}], turn_id="t1"),
    "approval.requested": lambda: ev.approval_requested(
        S, "r1", "tool", "Run rm?",
        options=[{"id": "allow_once", "label": "Allow", "kind": "allow_once"},
                 {"id": "allow_always", "label": "Always",
                  "kind": "allow_always", "standing": True}],
        call_id="call-1", turn_id="t1"),
    "approval.resolved": lambda: ev.approval_resolved(
        S, "r1", "user", decision="allow", by="iphone", turn_id="t1"),
    "input.requested": lambda: ev.input_requested(
        S, "q1", "Which branch?", kind="choice",
        options=[{"id": "main", "label": "main"}]),
    "plan.updated": lambda: ev.plan_updated(
        S, [{"text": "Read code", "status": "completed"},
            {"text": "Fix", "status": "in_progress"}], text="1. Read"),
    "task.updated": lambda: ev.task_updated(
        S, "task-1", "subagent", "running", parent_turn_id="t1",
        title="Explore", can_stop=True),
    "checkpoint.created": lambda: ev.checkpoint_created(
        S, "cp-1", files=3, turn_id="t1"),
    "checkpoint.restored": lambda: ev.checkpoint_restored(
        S, "cp-1", files=3, by="mac", command_id="c2"),
    "changes.updated": lambda: ev.changes_updated(
        S, [{"path": "/w/a.py", "kind": "modify", "added": 4, "removed": 1}]),
    "queue.updated": lambda: ev.queue_updated(
        S, [{"command_id": "c3", "text": "next", "by": "phone",
             "mode": "queue"}]),
    "command.receipt": lambda: ev.command_receipt(S, "c3", "queued",
                                                  op="message"),
    "command.result": lambda: ev.command_result(
        S, "c3", "message", True, note="sent", degraded=False),
    "usage.updated": lambda: ev.usage_updated(
        S, input_tokens=10, output_tokens=5, cost_usd=0.01),
    "context.compacted": lambda: ev.context_compacted(
        S, summary="earlier", tokens_before=180000),
    "error": lambda: ev.error(S, "agent_error", "boom", fatal=False),
    "notify": lambda: ev.notify(S, "finished", "done", status="done"),
}


class TestCatalogue(unittest.TestCase):
    def test_every_type_has_a_sample_builder(self):
        self.assertEqual(set(SAMPLES), ev.EVENT_TYPES)
        self.assertEqual(len(ev.EVENT_TYPES), 25)

    def test_samples_validate(self):
        for etype, build in SAMPLES.items():
            event = build()
            self.assertEqual(ev.validate(event), [], etype)
            self.assertEqual(event["type"], etype)
            self.assertEqual(event["v"], 2)

    def test_durable_is_everything_but_deltas(self):
        self.assertEqual(ev.DURABLE_TYPES, ev.EVENT_TYPES - {"message.delta"})
        self.assertFalse(ev.is_durable(SAMPLES["message.delta"]()))
        self.assertTrue(ev.is_durable(SAMPLES["turn.ended"]()))

    def test_enums_name_real_fields(self):
        for (etype, field), values in ev.ENUMS.items():
            self.assertIn(etype, ev.SCHEMAS)
            schema = ev.SCHEMAS[etype]
            head = field.split("[]")[0]
            spec = schema.required.get(head) or schema.optional.get(head)
            self.assertIsNotNone(spec, (etype, field))
            self.assertTrue(values)

    def test_notify_kinds_follow_v1(self):
        self.assertIs(ev.ENUMS[("notify", "kind")], protocol.NOTIFY_KINDS)

    def test_v1_field_names_kept(self):
        # A v2 delta's data carries every v1 delta field.
        v1 = protocol.delta_event(S, "mac", "m1", "assistant", "x", 1)
        self.assertLessEqual(set(v1["data"]),
                             set(ev.SCHEMAS["message.delta"].required)
                             | set(ev.SCHEMAS["message.delta"].optional))
        req = ev.SCHEMAS["approval.requested"]
        for name in ("request_id", "prompt", "options"):
            self.assertTrue(name in req.required or name in req.optional)


class TestEnvelope(unittest.TestCase):
    def test_shape(self):
        event = ev.turn_started(S, "t1", command_id="c1", ts=12.5)
        self.assertEqual(event, {"v": 2, "type": "turn.started",
                                 "session": S, "seq": 0, "ts": 12.5,
                                 "turn_id": "t1", "command_id": "c1",
                                 "data": {}})

    def test_delta_carries_no_seq(self):
        delta = SAMPLES["message.delta"]()
        self.assertNotIn("seq", delta)
        bad = dict(delta, seq=4)
        self.assertIn("message.delta is transient and carries no seq",
                      ev.validate(bad))

    def test_none_fields_are_dropped(self):
        event = ev.session_state(S, "idle")
        self.assertEqual(event["data"], {"state": "idle"})
        self.assertNotIn("turn_id", event)

    def test_turn_and_command_ids_required_where_they_apply(self):
        for etype, needs in (("turn.started", "turn_id"),
                             ("turn.ended", "turn_id"),
                             ("command.receipt", "command_id"),
                             ("command.result", "command_id")):
            event = SAMPLES[etype]()
            del event[needs]
            self.assertIn(f"{etype} needs a {needs}", ev.validate(event))

    def test_envelope_problems(self):
        self.assertEqual(ev.validate([]), ["event must be an object"])
        good = SAMPLES["notify"]()
        cases = [
            ({"v": 1}, "v must be 2"),
            ({"type": "message.appended"}, "unknown event type"),
            ({"session": ""}, "session must not be empty"),
            ({"ts": "now"}, "ts must be a non-negative number"),
            ({"seq": -1}, "seq must be a non-negative integer"),
            ({"seq": True}, "seq must be a non-negative integer"),
            ({"turn_id": 5}, "turn_id must be a string"),
            ({"machine": "mac"}, "machine is not an envelope field"),
        ]
        for patch, want in cases:
            problems = ev.validate({**good, **patch})
            self.assertTrue(any(want in p for p in problems),
                            (patch, problems))
        no_data = dict(good, type="bogus")
        no_data["data"] = None
        self.assertIn("data must be an object", ev.validate(no_data))

    def test_validate_does_not_change_the_event(self):
        event = SAMPLES["tool.completed"]()
        before = copy.deepcopy(event)
        ev.validate(event)
        self.assertEqual(event, before)


class TestDataProblems(unittest.TestCase):
    def problems(self, etype, **data_patch):
        event = SAMPLES[etype]()
        event["data"].update(data_patch)
        return ev.validate(event)

    def test_enum_values(self):
        self.assertTrue(self.problems("session.state", state="active"))
        self.assertTrue(self.problems("turn.ended", status="done"))
        self.assertTrue(self.problems("approval.requested", kind="file"))
        self.assertTrue(self.problems("approval.resolved", source="phone"))
        self.assertTrue(self.problems("approval.resolved", decision="yes"))
        self.assertTrue(self.problems("task.updated", kind="thread"))
        self.assertTrue(self.problems("command.receipt", status="ok"))
        self.assertTrue(self.problems("message.completed", kind="thinking"))
        self.assertTrue(self.problems("notify", kind="ping"))

    def test_nested_items(self):
        self.assertTrue(self.problems(
            "tool.completed", file_edits=[{"path": "/a", "kind": "move"}]))
        self.assertTrue(self.problems(
            "tool.completed", file_edits=[{"kind": "add"}]))
        self.assertTrue(self.problems("tool.completed", file_edits="a.py"))
        self.assertTrue(self.problems(
            "approval.requested", options=[{"label": "no id"}]))
        self.assertTrue(self.problems(
            "approval.requested", options=["Allow"]))
        self.assertTrue(self.problems(
            "plan.updated", items=[{"text": "a", "status": "doing"}]))
        self.assertTrue(self.problems(
            "queue.updated", items=[{"command_id": "c", "text": "x",
                                     "mode": "later"}]))

    def test_types_and_unknown_fields(self):
        self.assertTrue(self.problems("message.delta", seq=0.5))
        self.assertTrue(self.problems("message.delta", done="yes"))
        self.assertTrue(self.problems("thinking.completed", duration_s=-1))
        self.assertTrue(self.problems("message.completed", text=None))
        self.assertTrue(self.problems("message.completed", colour="red"))
        self.assertIn("data.colour is not a known field",
                      self.problems("message.completed", colour="red"))
        event = SAMPLES["message.completed"]()
        del event["data"]["text"]
        self.assertIn("data.text is required", ev.validate(event))

    def test_make_raises_with_every_problem(self):
        with self.assertRaises(ValueError) as ctx:
            ev.make("turn.ended", S, {"status": "done"})
        self.assertIn("needs a turn_id", str(ctx.exception))
        self.assertIn("data.status", str(ctx.exception))
        with self.assertRaises(ValueError):
            ev.usage_updated(S, tokens=5)


if __name__ == "__main__":
    unittest.main()
