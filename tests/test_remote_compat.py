"""v2 events served as v1 frames (remote/protocol_compat.py), checked
against the v1 shapes protocol.py, tail.py and acp.py build today."""

import copy
import unittest

from swe.remote import (
    acp, attachments, protocol, protocol_compat as compat, tail,
)
from swe.remote.adapters import events as ev
from swe.sessions import status as session_status
from tests.test_remote_events_v2 import SAMPLES

S = "claude:abc"
M = "macbook"
TS = 1_760_000_000.0


def stamped(event, seq=7, ts=TS):
    out = dict(event, ts=ts)
    if "seq" in out:
        out["seq"] = seq
    return out


def one(event):
    got = compat.to_v1(event, machine=M)
    assert len(got) == 1, got
    return got[0]


class TestMessages(unittest.TestCase):
    def test_message_completed(self):
        for kind in ("user", "assistant", "system"):
            v2 = stamped(ev.message_completed(S, "m1", kind, "Hello",
                                              turn_id="t1"))
            want = protocol.event("message.appended", S, M,
                                  protocol.message(kind, "Hello", ts=TS),
                                  ts=TS)
            want["seq"] = 7
            self.assertEqual(one(v2), want)

    def test_user_attachments_match_the_v1_send_path(self):
        files = ["/w/inbox/a b.png", "/w/inbox/c.pdf"]
        v2 = stamped(ev.message_completed(S, "m1", "user", "Look at these",
                                          attachments=files, by="iphone"))
        # attachments.prompt_text is what inject._prompt sends, and so
        # what the transcript's message.appended records.
        plain_reg = {"codex": {}}
        at_reg = {"claude": {}}
        for at_refs, harness, reg in ((False, "codex", plain_reg),
                                      (True, "claude", at_reg)):
            want = attachments.prompt_text("Look at these", files,
                                           harness, reg)
            self.assertEqual(attachments.at_refs(harness, reg), at_refs)
            got = compat.to_v1(v2, machine=M, at_refs=at_refs)[0]
            self.assertEqual(got["data"],
                             protocol.message("user", want, ts=TS))
        plain = one(v2)["data"]["text"]
        self.assertEqual(plain, "[Attached file: /w/inbox/a b.png] "
                                "[Attached file: /w/inbox/c.pdf] "
                                "Look at these")
        refs = compat.stream_v1([v2], machine=M, at_refs=True)[0]
        self.assertEqual(refs["data"]["text"],
                         "@/w/inbox/a b.png @/w/inbox/c.pdf Look at these")
        empty = stamped(ev.message_completed(S, "m1", "user", "",
                                             attachments=["/w/a.png"]))
        self.assertEqual(one(empty)["data"]["text"],
                         attachments.prompt_text("", ["/w/a.png"], "codex",
                                                 plain_reg))
        self.assertEqual(compat.message_text("hi", None), "hi")

    def test_thinking_matches_tail(self):
        v2 = stamped(ev.thinking_completed(S, "m2", "pondering",
                                           duration_s=3.5))
        self.assertEqual(one(v2)["data"], tail._thinking("pondering", TS, 3.5))
        bare = stamped(ev.thinking_completed(S, "m2", "pondering"))
        self.assertEqual(one(bare)["data"],
                         tail._thinking("pondering", TS, None))

    def test_tool_cards_match_tails_decode(self):
        args = {"command": "ls -la"}
        record = [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                   "input": args},
                  {"type": "tool_result", "tool_use_id": "toolu_1",
                   "content": "a\nb"}]
        use_v1, result_v1 = tail._decode_blocks("assistant", record, TS)
        summary, _full = tail._summarize_args("Bash", args)
        started = stamped(ev.tool_started(S, "toolu_1", "Bash",
                                          summary=summary, input=args))
        completed = stamped(ev.tool_completed(S, "toolu_1", "ok",
                                              output="a\nb"))
        self.assertEqual(one(started)["data"], use_v1)
        self.assertEqual(one(completed)["data"], result_v1)

    def test_tool_card_defaults(self):
        bare = one(stamped(ev.tool_started(S, "c1", "Read")))
        self.assertEqual(bare["data"]["tool"], {
            "name": "Read", "call_id": "c1", "args_summary": "Read",
            "args_full": ""})
        raw = one(stamped(ev.tool_started(S, "c1", "Read", input="{}")))
        self.assertEqual(raw["data"]["tool"]["args_full"], "{}")
        empty = one(stamped(ev.tool_completed(S, "c1", "error")))
        self.assertEqual(empty["data"]["text"], "")
        self.assertEqual(empty["data"]["result_ref"], "c1")

    def test_compaction_and_error(self):
        compacted = one(stamped(ev.context_compacted(S)))
        self.assertEqual(compacted["data"],
                         protocol.message("compaction", "context compacted",
                                          ts=TS))
        summary = one(stamped(ev.context_compacted(S, summary="earlier")))
        self.assertEqual(summary["data"]["text"], "earlier")
        err = one(stamped(ev.error(S, "agent_error", "boom")))
        self.assertEqual(err["data"], protocol.message("error", "boom", ts=TS))

    def test_kinds_are_ones_v1_accepts(self):
        for build in SAMPLES.values():
            for out in compat.to_v1(stamped(build()), machine=M):
                if out["type"] in ("message.appended", "message.delta"):
                    self.assertIn(out["data"]["kind"],
                                  protocol.MESSAGE_KINDS)
                self.assertIn(out["type"],
                              protocol.EVENT_TYPES | {"command.result"})
                self.assertEqual(out["v"], protocol.PROTOCOL_VERSION)
                self.assertEqual(out["machine"], M)


class TestLiveText(unittest.TestCase):
    def test_delta(self):
        v2 = dict(ev.message_delta(S, "m1", "assistant", "Hel", 3,
                                   offset=10, done=True), ts=TS)
        want = protocol.delta_event(S, M, "m1", "assistant", "Hel", 3,
                                    done=True, ts=TS)
        self.assertEqual(one(v2), want)
        self.assertNotIn("seq", one(v2))


class TestStatus(unittest.TestCase):
    def test_states(self):
        want = {"running": session_status.ACTIVE,
                "idle": session_status.DONE,
                "waiting": session_status.FOLLOWUP,
                "agent_down": session_status.ERROR,
                "parked": session_status.DONE,
                "closed": session_status.DONE}
        for state, status in want.items():
            out = one(stamped(ev.session_state(S, state, reason="x")))
            self.assertEqual(out["type"], "session.status")
            self.assertEqual(out["data"], {"status": status})
        self.assertEqual(compat.to_v1(stamped(ev.session_state(
            S, "starting")), machine=M), [])
        self.assertEqual(set(compat.STATUS_FOR_STATE) | {"starting"},
                         ev.SESSION_STATES)


class TestApprovals(unittest.TestCase):
    def test_requested_matches_acp_menu_rows(self):
        acp_opts = [{"optionId": "a1", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "a2", "name": "Always",
                     "kind": "allow_always"},
                    {"optionId": "r1", "name": "Deny"}]
        rows = [acp._menu_row(i, o) for i, o in enumerate(acp_opts, 1)]
        v2_opts = [{"id": "a1", "label": "Allow", "kind": "allow_once"},
                   {"id": "a2", "label": "Always", "kind": "allow_always",
                    "standing": True},
                   {"id": "r1", "label": "Deny"}]
        v2 = stamped(ev.approval_requested(S, "acp-1", "tool", "Run rm?",
                                           options=v2_opts))
        self.assertEqual(one(v2)["data"], {"request_id": "acp-1",
                                           "prompt": "Run rm?",
                                           "options": rows})

    def test_requested_auto_and_no_options(self):
        auto = one(stamped(ev.approval_requested(S, "r1", "plan", "Plan?",
                                                 auto=True)))
        self.assertEqual(auto["data"], {"request_id": "r1", "prompt": "Plan?",
                                        "options": [], "auto": True})
        unlabelled = one(stamped(ev.approval_requested(
            S, "r1", "tool", "x", options=[{"id": "o1", "label": ""}])))
        self.assertEqual(unlabelled["data"]["options"],
                         [{"n": "1", "label": "o1"}])

    def test_resolved(self):
        by_user = one(stamped(ev.approval_resolved(
            S, "r1", "user", decision="allow", by="iphone")))
        # hub.py's own resolve: request_id and decision, no source.
        want = protocol.event("approval.resolved", S, M,
                              {"request_id": "r1", "decision": "allow"},
                              ts=TS)
        want["seq"] = 7
        self.assertEqual(by_user, want)
        retired = one(stamped(ev.approval_resolved(S, "r1", "turn_ended")))
        self.assertEqual(retired["data"], {"request_id": "r1",
                                           "source": "turn_ended"})


class TestCommandsAndNotify(unittest.TestCase):
    def test_command_result_settles_v1_bubbles(self):
        v2 = stamped(ev.command_result(S, "c-1", "message", False,
                                       note="refused", cancelled=True,
                                       request_id="r1", launch_id="l1"))
        self.assertEqual(one(v2)["data"], {
            "command_id": "c-1", "op": "message", "ok": False,
            "note": "refused", "degraded": False, "client_id": "c-1",
            "request_id": "r1", "launch_id": "l1", "cancelled": True})
        plain = one(stamped(ev.command_result(S, "c-2", "interrupt", True)))
        self.assertEqual(plain["data"], {
            "command_id": "c-2", "op": "interrupt", "ok": True, "note": "",
            "degraded": False, "client_id": "c-2"})

    def test_notify(self):
        out = one(stamped(ev.notify(S, "error", "boom", status="error")))
        self.assertEqual(out["data"], {"kind": "error", "message": "boom",
                                       "status": "error"})
        bare = one(stamped(ev.notify(S, "finished", "done")))
        self.assertEqual(bare["data"], {"kind": "finished", "message": "done"})


class TestMapping(unittest.TestCase):
    def test_every_type_is_mapped_or_dropped(self):
        mapped = {t for t, build in SAMPLES.items()
                  if compat.to_v1(stamped(build()), machine=M)}
        self.assertEqual(mapped | compat.DROPPED, ev.EVENT_TYPES)
        self.assertFalse(mapped & compat.DROPPED)

    def test_at_most_one_frame_keeping_seq(self):
        for etype, build in SAMPLES.items():
            v2 = stamped(build(), seq=41)
            got = compat.to_v1(v2, machine=M)
            self.assertLessEqual(len(got), 1, etype)
            for out in got:
                if etype == "message.delta":
                    self.assertNotIn("seq", out)
                else:
                    self.assertEqual(out["seq"], 41, etype)
                    self.assertEqual(out["ts"], TS)
                    self.assertEqual(out["session"], S)

    def test_pure(self):
        for build in SAMPLES.values():
            v2 = stamped(build())
            before = copy.deepcopy(v2)
            compat.to_v1(v2, machine=M)
            self.assertEqual(v2, before)

    def test_refuses_non_v2(self):
        v1 = protocol.event("notify", S, M, {"kind": "error"})
        for bad in (v1, None, {"v": 2, "type": "bogus", "data": {}}):
            with self.assertRaises(ValueError):
                compat.to_v1(bad)

    def test_stream_order(self):
        run = [stamped(SAMPLES["turn.started"](), seq=1),
               stamped(SAMPLES["message.completed"](), seq=2),
               stamped(SAMPLES["plan.updated"](), seq=3),
               stamped(SAMPLES["session.state"](), seq=4)]
        out = compat.stream_v1(run, machine=M)
        self.assertEqual([(e["type"], e["seq"]) for e in out],
                         [("message.appended", 2), ("session.status", 4)])


if __name__ == "__main__":
    unittest.main()
