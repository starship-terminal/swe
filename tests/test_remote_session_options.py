"""G32: a running session's options (docs/specs/G32-session-options.md).

One test per row of the spec's table (each adapter crossed with each
option: listed or not, the value it reports, when a change takes
effect, what a refusal looks like), from the recorded fixtures where
one covers the row; then the keeper (the value stays true through a
change, a refusal, the agent's own change, a park and a keeper
restart), and the hub (the snapshot, the stream, ``/v1/status``).
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from swe.remote import protocol, sync as sync_mod
from swe.remote.adapters import acp, claude_wire, codex_wire, opencode_wire
from swe.remote.adapters import events as ev
from swe.remote.adapters import fake, options as opts
from swe.remote.adapters.base import AdapterError, Caps, TurnInput
from swe.remote.adapters.core import ALL_CAPS

from tests.test_remote_acp import _AcpAdapterCase
from tests.test_remote_adapters import _CodexCase, _OpencodeCase
from tests.test_remote_keeper import KeeperCase, S, kill
from tests.test_remote_sse_v2 import V2Case

PROTOCOLS = Path(__file__).resolve().parent / "fixtures" / "protocols"


def _lines(adapter: str, version: str, scenario: str) -> list[dict]:
    path = PROTOCOLS / adapter / version / f"{scenario}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _options(events: list[dict]) -> list[dict]:
    return [e["data"]["options"] for e in events
            if e["type"] == "session.options"]


def _values(events: list[dict], name: str) -> list:
    """Each value ``name`` took, in order, without repeats."""
    out: list = []
    for shown in _options(events):
        if name in shown and (not out or out[-1] != shown[name]["value"]):
            out.append(shown[name]["value"])
    return out


# ---------------------------------------------------------------------
# The table, per adapter


class TestClaude(unittest.TestCase):
    """claude_stream 2.1.328: model listed, permission mode and plan
    mode current only, effort not offered; every change sent at once."""

    def replay(self, scenario: str) -> list[dict]:
        return claude_wire.replay(_lines("claude_stream", "2.1.328",
                                         scenario))

    def test_model_is_listed_and_shown_as_the_listed_id(self):
        events = self.replay("set_model")
        first = _options(events)[0]["model"]
        # The handshake's own list, in its order: nothing written here.
        self.assertEqual([c["id"] for c in first["choices"]],
                         ["default", "opus", "fable", "sonnet", "haiku"])
        self.assertEqual(first["mid_turn"], "reply_finishes")
        # The CLI answers ``sonnet`` with ``claude-sonnet-5-5``; the
        # listed id is the value, the resolved one ``reported``.
        self.assertEqual(_values(events, "model"),
                         ["haiku", "sonnet", "haiku"])
        last = _options(events)[-1]["model"]
        self.assertEqual((last["value"], last["reported"]),
                         ("haiku", "claude-haiku-5-5"))

    def test_effort_is_never_offered(self):
        # No control request changes it mid-session (G7): PROVEN keeps
        # it out of the caps, so no event names it.
        self.assertNotIn("effort", claude_wire.PROVEN.options)
        for scenario in ("hello", "set_model", "plan_mode"):
            for shown in _options(self.replay(scenario)):
                self.assertNotIn("effort", shown)

    def test_permission_and_plan_mode_follow_set_permission_mode(self):
        events = self.replay("plan_mode")
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])
        self.assertEqual(_values(events, "permission_mode"),
                         ["default", "plan", "default"])
        # The CLI lists no permission modes: the value only.
        self.assertNotIn("choices", _options(events)[0]["permission_mode"])

    def test_the_cli_leaving_plan_mode_by_itself_is_shown(self):
        # plan_approve: started in plan mode, the plan approved, and the
        # CLI's own status line says ``default``: nothing of ours set it.
        lines = _lines("claude_stream", "2.1.328", "plan_approve")
        sent = [line["msg"]["request"]["subtype"] for line in lines
                if line.get("dir") == "out"
                and line.get("msg", {}).get("type") == "control_request"]
        self.assertNotIn("set_permission_mode", sent)
        self.assertEqual(_values(self.replay("plan_approve"), "plan_mode"),
                         [True, False])

    def test_display_names_are_taken_where_the_cli_sends_them(self):
        hs = claude_wire.Handshake.from_body({"models": [
            {"value": "sonnet", "displayName": "Sonnet 5.5",
             "description": "everyday"},
            {"value": "haiku", "displayName": "haiku"},
            {"displayName": "no value"}]})
        self.assertEqual(hs.models, ("sonnet", "haiku"))
        self.assertEqual(hs.model_choices, (
            {"id": "sonnet", "label": "Sonnet 5.5",
             "description": "everyday"},
            {"id": "haiku"}))

    def state(self) -> claude_wire.StreamState:
        st = claude_wire.StreamState("claude:x", clock=lambda: 1.0,
                                     run_id="aaaa0001")
        st.model_choices = (opts.choice("default"), opts.choice("sonnet"))
        st.start_events(claude_wire.PROVEN, "sid")
        return st

    def test_a_model_the_cli_switched_to_by_itself_shows_as_it_is(self):
        st = self.state()
        out = st.feed({"type": "system", "subtype": "init",
                       "model": "claude-opus-5-5"}).events
        self.assertEqual(_options(out)[-1]["model"]["value"], "default",
                         "no --model: the first model named is the default")
        # A later init naming a model nobody asked for (a fallback):
        # shown as the CLI names it, matching no listed id.
        out = st.feed({"type": "system", "subtype": "init",
                       "model": "claude-sonnet-4-9"}).events
        self.assertEqual(_options(out)[-1]["model"]["value"],
                         "claude-sonnet-4-9")

    def test_a_refused_set_model_changes_nothing(self):
        st = self.state()
        st.model_asked("r1", "sonnet")
        fed = st.feed({"type": "control_response", "response": {
            "subtype": "error", "request_id": "r1",
            "error": "Model 'sonnet' is not available"}})
        self.assertEqual(_options(fed.events), [])
        self.assertEqual(fed.acks[0].error, "Model 'sonnet' is not available")


class TestAcp(unittest.TestCase):
    """acp (qwen-code 0.28.0): models and modes listed by the agent,
    plan mode is its ``plan`` mode, effort not offered."""

    def replay(self, scenario: str) -> list[dict]:
        return acp.replay(_lines("acp", "qwen-code-0.28.0", scenario))

    def test_models_are_the_agents_list(self):
        events = self.replay("model")
        model = _options(events)[0]["model"]
        self.assertEqual(model["choices"], [{
            "id": "$runtime|openai|gpt-4o-mini(openai)",
            "label": "gpt-4o-mini"}])
        self.assertEqual(model["value"], "$runtime|openai|gpt-4o-mini(openai)")
        self.assertEqual(model["mid_turn"], "unverified")
        for shown in _options(events):
            self.assertNotIn("effort", shown)

    def test_the_mode_is_a_value_only_and_plan_mode_is_one_of_them(self):
        events = self.replay("plan_mode")
        mode = _options(events)[0]["permission_mode"]
        # The agent lists yolo and auto among its modes: a page is never
        # handed them as choices (G32, Limits).
        self.assertNotIn("choices", mode)
        self.assertEqual(_values(events, "permission_mode"),
                         ["auto", "plan", "auto"])
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])

    def test_the_agent_changing_its_own_mode_is_shown(self):
        st = acp.AcpState("acp:x", clock=lambda: 1.0, run_id="aaaa0001")
        st.session_info({"sessionId": "s", "modes": {
            "currentModeId": "plan", "availableModes": [
                {"id": "plan"}, {"id": "default"}]}})
        st.session_id = "s"
        st.start_events(ALL_CAPS.narrowed(acp.PROVEN), "s")
        out = st.update({"sessionId": "s", "update": {
            "sessionUpdate": "current_mode_update",
            "currentModeId": "default"}}).events
        self.assertEqual(_options(out)[-1]["plan_mode"]["value"], False)


class TestCodex(unittest.TestCase):
    """codex_appserver 0.170.0: model and effort current only, the
    approval policies the API's enum; every change waits for the next
    turn."""

    def test_values_follow_the_turns_that_carry_them(self):
        events = codex_wire.replay(_lines("codex_appserver", "0.170.0",
                                          "options"))
        first = _options(events)[0]
        self.assertEqual(set(first), {"model", "effort", "permission_mode"},
                         "no plan mode")
        for shown in _options(events):
            for option in shown.values():
                self.assertEqual(option["mid_turn"], "next_turn")
        self.assertNotIn("choices", first["model"])
        # The policy is a value only: ``never`` is never offered.
        self.assertNotIn("choices", first["permission_mode"])
        self.assertEqual(_values(events, "model"),
                         ["gpt-5.6-luna", "gpt-6-luna"])
        self.assertEqual(_values(events, "permission_mode"),
                         ["untrusted", "never"])


class TestOpencode(unittest.TestCase):
    """opencode_serve 2.3.2: model and its variant (effort) current
    only, plan mode the ``plan`` agent, no permission mode."""

    def replay(self, scenario: str) -> list[dict]:
        return opencode_wire.replay(_lines("opencode_serve", "2.3.2",
                                           scenario))

    def test_model_and_effort(self):
        events = self.replay("options")
        first = _options(events)[0]
        self.assertEqual(set(first), {"model", "effort", "plan_mode"})
        self.assertNotIn("choices", first["model"])
        self.assertEqual(_values(events, "model"),
                         ["openai/gpt-5.4-nano", "openai/gpt-5.4-mini"])
        self.assertEqual(_values(events, "effort"), ["low", "medium", "low"])

    def test_plan_mode_is_the_plan_agent(self):
        self.assertEqual(_values(self.replay("plan_mode"), "plan_mode"),
                         [False, True, False])

    def test_the_server_switching_agent_by_itself_is_shown(self):
        st = opencode_wire.ServeState("oc:x", clock=lambda: 1.0,
                                      run_id="aaaa0001")
        st.session_id = "ses_1"
        st.agent = "plan"
        st.start_events(ALL_CAPS.narrowed(opencode_wire.PROVEN), "ses_1")
        out = st.feed({"type": "session.agent.selected", "data": {
            "sessionID": "ses_1", "agent": "build"}}).events
        self.assertEqual(_options(out)[-1]["plan_mode"]["value"], False)


class TestOpencodeLateReports(unittest.TestCase):
    """A server report of an earlier post, landing after a newer post,
    never undoes the newer change (G32)."""

    def state(self):
        st = opencode_wire.ServeState("oc:x", clock=lambda: 1.0,
                                      run_id="aaaa0001")
        st.session_id = "ses_1"
        st.model = {"providerID": "p", "id": "m0"}
        st.start_events(ALL_CAPS.narrowed(opencode_wire.PROVEN), "ses_1")
        return st

    def report(self, st, model_id):
        return st.feed({"type": "session.model.selected", "data": {
            "sessionID": "ses_1",
            "model": {"providerID": "p", "id": model_id}}}).events

    def test_a_late_report_of_the_earlier_model_is_ignored(self):
        st = self.state()
        said = st.model_selected({"providerID": "p", "id": "a"})
        said += st.model_selected({"providerID": "p", "id": "b"})
        said += self.report(st, "a")
        said += self.report(st, "b")
        self.assertEqual(_values(said, "model"), ["p/a", "p/b"])
        self.assertEqual(st.model["id"], "b")
        # Once its post is reported back, the server's own change shows.
        self.assertEqual(_values(self.report(st, "c"), "model"), ["p/c"])

    def test_when_the_newer_report_never_comes_the_newer_stays(self):
        st = self.state()
        said = st.model_selected({"providerID": "p", "id": "a"})
        said += st.model_selected({"providerID": "p", "id": "b"})
        said += self.report(st, "a")
        self.assertEqual(_values(said, "model"), ["p/a", "p/b"])
        self.assertEqual(_options(said)[-1]["model"]["value"], "p/b")

    def agent_report(self, st, agent):
        return st.feed({"type": "session.agent.selected", "data": {
            "sessionID": "ses_1", "agent": agent}}).events

    def test_the_server_leaving_plan_after_a_post_is_shown(self):
        # The plan post is never echoed; mid-turn the server leaves plan
        # by itself: that is shown, and stays after the turn.
        st = self.state()
        said = st.agent_selected("plan")
        said += self.agent_report(st, "build")
        self.assertEqual(_values(said, "plan_mode"), [True, False])
        self.assertEqual(st.agent, "build")

    def test_a_model_the_server_picks_while_a_post_is_pending_is_shown(self):
        st = self.state()
        said = st.model_selected({"providerID": "p", "id": "b"})
        said += self.report(st, "fallback")
        self.assertEqual(_values(said, "model"), ["p/b", "p/fallback"])

    def test_a_b_a_posted_quickly_ends_on_a_whatever_the_echo_order(self):
        import itertools
        for order in sorted(set(itertools.permutations("aba"))):
            with self.subTest(echoes="".join(order)):
                st = self.state()
                said = []
                for model_id in "aba":
                    said += st.model_selected({"providerID": "p",
                                               "id": model_id})
                for model_id in order:
                    said += self.report(st, model_id)
                self.assertEqual(_values(said, "model"),
                                 ["p/a", "p/b", "p/a"], "echoes say nothing")
                self.assertEqual(st.model["id"], "a")
                # Every echo is used up: the server's next word shows.
                self.assertEqual(_values(self.report(st, "b"), "model"),
                                 ["p/b"])

    def test_a_late_agent_report_is_ignored(self):
        st = self.state()
        said = st.agent_selected("plan") + st.agent_selected("build")
        said += st.feed({"type": "session.agent.selected", "data": {
            "sessionID": "ses_1", "agent": "plan"}}).events
        self.assertEqual(_values(said, "plan_mode"), [True, False])


class TestAcpModeReports(unittest.TestCase):
    """ACP's ``current_mode_update`` is always the agent's word (G32)."""

    def test_the_agent_leaving_plan_mode_after_a_set_is_shown(self):
        st = acp.AcpState("acp:x", clock=lambda: 1.0, run_id="aaaa0001")
        st.session_id = "s1"
        st.modes = ["default", "plan"]
        st.mode = "default"
        st.start_events(ALL_CAPS.narrowed(acp.PROVEN), "s1")
        said = st.mode_set("plan")  # the agent never echoes it
        said += st.update({"sessionId": "s1", "update": {
            "sessionUpdate": "current_mode_update",
            "currentModeId": "default"}}).events  # a plan approved
        self.assertEqual(_values(said, "plan_mode"), [True, False])
        self.assertEqual(st.mode, "default")


class TestKeeperCarriesTheModelList(unittest.TestCase):
    """A value-only ``session.options`` keeps the model list shown
    before it, and only that list (G32)."""

    def carry(self, before, options):
        from swe.remote.keeper.process import Keeper
        k = object.__new__(Keeper)
        k.session_key = "acp:x"
        k._shown_options = before
        return Keeper._carry_choices(
            k, ev.session_options("acp:x", options))["data"]["options"]

    def test_the_model_list_is_carried(self):
        listed = [{"id": "m1"}, {"id": "m2"}]
        out = self.carry({"model": {"value": "m1", "mid_turn": "next_turn",
                                    "choices": listed}},
                         {"model": {"value": "m2", "mid_turn": "next_turn"}})
        self.assertEqual(out["model"]["choices"], listed)

    def test_a_permission_list_in_an_older_log_is_not_carried(self):
        modes = [{"id": m} for m in ("plan", "default", "auto", "yolo")]
        out = self.carry(
            {"permission_mode": {"value": "default",
                                 "mid_turn": "reply_finishes",
                                 "choices": modes}},
            {"permission_mode": {"value": "default",
                                 "mid_turn": "reply_finishes"}})
        self.assertNotIn("choices", out["permission_mode"])


class TestOpencodeAdapterLateReports(_OpencodeCase):
    def test_a_model_the_server_never_reports_back_stays_shown(self):
        adapter, sink, _sid = self.started("silent-model",
                                           spec={"model": "prov/m1"})
        adapter.set_option("model", "prov/m2")
        adapter.set_option("model", "prov/m3")
        self.wait_turn(sink, adapter.send_turn(TurnInput("go")))
        said = [e["data"]["options"]["model"]["value"]
                for e in sink.events if e["type"] == "session.options"]
        self.assertEqual(said[-1], "prov/m3")


def _notes(adapter: str, version: str, scenario: str) -> dict:
    return _lines(adapter, version, scenario)[0]["notes"]


class TestMidTurn(unittest.TestCase):
    """What a change sent while a reply streams does, from the G32
    recordings (each a counting reply, the change sent after its first
    deltas)."""

    def test_claude_model_the_reply_finishes_on_the_old_model(self):
        lines = _lines("claude_stream", "2.1.330", "set_model_mid")
        models = [line["msg"]["message"]["model"] for line in lines
                  if line.get("dir") == "in"
                  and line.get("msg", {}).get("type") == "assistant"]
        # The reply in progress, then the next turn's.
        self.assertEqual(models, ["claude-haiku-5-5", "claude-sonnet-5-5"])
        notes = lines[0]["notes"]
        self.assertEqual((notes["answered"], notes["result_before_answer"],
                          notes["stop_reason"], notes["reply_lines"]),
                         ("success", False, "end_turn", 150))
        events = claude_wire.replay(lines)
        self.assertEqual(_values(events, "model"), ["haiku", "sonnet"])
        changed = next(i for i, e in enumerate(events)
                       if e["type"] == "session.options"
                       and e["data"]["options"]["model"]["value"] == "sonnet")
        reply = next(i for i, e in enumerate(events)
                     if e["type"] == "message.completed"
                     and e["data"]["kind"] == "assistant")
        self.assertLess(changed, reply, "the change was taken mid-reply")
        self.assertEqual(_options(events)[0]["model"]["mid_turn"],
                         "reply_finishes")

    def test_claude_plan_mode_the_reply_finishes(self):
        notes = _notes("claude_stream", "2.1.330", "plan_mode_mid")
        self.assertEqual((notes["answered"], notes["result_before_answer"],
                          notes["stop_reason"], notes["reply_lines"]),
                         ("success", False, "end_turn", 150))
        events = claude_wire.replay(_lines("claude_stream", "2.1.330",
                                           "plan_mode_mid"))
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])
        for option in _options(events)[0].values():
            self.assertEqual(option["mid_turn"], "reply_finishes")

    def test_acp_mode_the_reply_finishes(self):
        notes = _notes("acp", "qwen-code-0.28.0", "plan_mode_mid")
        self.assertTrue(notes["answered"])
        self.assertEqual(notes["stop"], "end_turn")
        self.assertGreater(notes["chunks_after_change"], 0, "not cut")
        events = acp.replay(_lines("acp", "qwen-code-0.28.0",
                                   "plan_mode_mid"))
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])
        shown = _options(events)[0]
        self.assertEqual((shown["permission_mode"]["mid_turn"],
                          shown["plan_mode"]["mid_turn"]),
                         ("reply_finishes", "reply_finishes"))

    def test_acp_model_stays_unverified(self):
        # Taken mid-prompt and the reply went on, but the recording's
        # agent lists one model: it was set to the one it ran, so no
        # switch could show.
        notes = _notes("acp", "qwen-code-0.28.0", "model_mid")
        self.assertEqual((notes["answered"], notes["stop"]),
                         (True, "end_turn"))
        self.assertGreater(notes["chunks_after_change"], 0)
        events = acp.replay(_lines("acp", "qwen-code-0.28.0", "model_mid"))
        self.assertEqual(len(_options(events)[0]["model"]["choices"]), 1)
        self.assertEqual(_options(events)[0]["model"]["mid_turn"],
                         "unverified")

    def test_opencode_model_the_step_finishes_on_the_old_model(self):
        notes = _notes("opencode_serve", "2.3.2", "model_mid")
        self.assertEqual(notes["answer_status"], 204)
        self.assertEqual(notes["end"], "session.execution.succeeded")
        self.assertGreater(notes["deltas_after_change"], 0, "not cut")
        self.assertEqual([s["model"]["id"] for s in notes["steps"]],
                         ["gpt-5.4-nano"])
        self.assertEqual([m["id"] for m in notes["next_steps"]],
                         ["gpt-5.4-mini"])
        events = opencode_wire.replay(_lines("opencode_serve", "2.3.2",
                                             "model_mid"))
        self.assertEqual(_values(events, "model"),
                         ["openai/gpt-5.4-nano", "openai/gpt-5.4-mini"])
        self.assertEqual(_options(events)[0]["model"]["mid_turn"],
                         "reply_finishes")

    def test_opencode_plan_mode_the_step_finishes_then_one_runs_as_plan(self):
        notes = _notes("opencode_serve", "2.3.2", "plan_mode_mid")
        self.assertGreater(notes["deltas_after_change"], 0, "not cut")
        # The step in progress ran as build to its end; the switch then
        # ran a step of its own, as plan, in the same execution.
        self.assertEqual([s["agent"] for s in notes["steps"]],
                         ["build", "plan"])
        events = opencode_wire.replay(_lines("opencode_serve", "2.3.2",
                                             "plan_mode_mid"))
        self.assertEqual(_values(events, "plan_mode"), [False, True, False])
        self.assertEqual(
            [e["data"]["status"] for e in events if e["type"] == "turn.ended"],
            ["completed", "completed"], "one turn for the reply and the step")


class TestModelLists(unittest.TestCase):
    """The lists Codex and opencode give when asked (G32 recordings):
    ids and display names, nothing else kept."""

    def test_codex_model_list(self):
        events = codex_wire.replay(_lines("codex_appserver", "0.171.0",
                                          "model_list"))
        model = _options(events)[-1]["model"]
        self.assertEqual(len(model["choices"]), 11)
        self.assertEqual(model["choices"][0], {"id": "gpt-6.2-sol",
                                               "label": "GPT-6.2-Sol"})
        self.assertIn("gpt-5.6-luna", [c["id"] for c in model["choices"]])
        self.assertEqual(model["value"], "gpt-5.6-luna")

    def test_opencode_model_list(self):
        lines = _lines("opencode_serve", "2.3.2", "providers")
        self.assertEqual(lines[0]["notes"]["paths"], {
            "/api/config/providers": 404, "/api/provider": 200,
            "/api/model": 200})
        events = opencode_wire.replay(lines)
        model = _options(events)[-1]["model"]
        self.assertEqual(model["value"], "openai/gpt-5.4-nano")
        ids = [c["id"] for c in model["choices"]]
        self.assertIn("openai/gpt-5.4-nano", ids)
        self.assertTrue(all("/" in i for i in ids))
        # Nothing but ids and names was committed.
        for line in lines:
            body = (line.get("http") or {}).get("body") or {}
            for item in body.get("data") or [] \
                    if isinstance(body.get("data"), list) else []:
                self.assertLessEqual(set(item), {"id", "providerID", "name"})

    def test_a_server_that_lists_nothing_leaves_the_value_alone(self):
        st = codex_wire.ThreadState("codex:x", clock=lambda: 1.0,
                                    run_id="aaaa0001")
        st.thread_settings({"model": "m"})
        st.start_events(codex_wire.PROVEN, "t")
        st.model_list({})
        self.assertNotIn("choices", st.options()["model"])
        self.assertEqual(opencode_wire.model_choices({"data": [
            {"id": "x"}, {"providerID": "p", "id": "y", "name": "Y"}]}),
            [{"id": "p/y", "label": "Y"}])


class TestOpencodeListAfterStart(_OpencodeCase):
    """The list is asked after the start and arrives as one more
    session.options: a slow one never holds the start up."""

    def test_the_servers_models_arrive_after_the_start(self):
        _adapter, sink, _sid = self.started("models")
        found = sink.wait_for(lambda e: e["type"] == "session.options"
                              and "choices" in e["data"]["options"]["model"])
        self.assertIsNotNone(found)
        self.assertEqual(found["data"]["options"]["model"]["choices"], [
            {"id": "fake/model-1", "label": "One"},
            {"id": "fake/model-2", "label": "Two"}])

    def test_a_slow_list_does_not_hold_the_start(self):
        started = time.monotonic()
        _adapter, sink, _sid = self.started("slow-models")
        self.assertLess(time.monotonic() - started, 1.4,
                        "start returned before the list")
        self.assertNotIn("choices", _options(sink.events)[0]["model"])
        found = sink.wait_for(lambda e: e["type"] == "session.options"
                              and "choices" in e["data"]["options"]["model"],
                              timeout=10.0)
        self.assertIsNotNone(found, "the late list still arrives")

    def test_a_list_that_arrives_for_a_closed_adapter_is_dropped(self):
        import types
        adapter, sink, _sid = self.started()
        seen: list[int] = []

        def call(request):
            adapter.close()  # closed while the list was on its way
            seen.append(len(sink.events))
            return types.SimpleNamespace(ok=True, body={"data": [
                {"id": "model-1", "providerID": "fake", "name": "One"}]})

        adapter._call = call
        adapter._fetch_models()
        self.assertEqual(len(seen), 1)
        self.assertEqual(sink.events[seen[0]:], [], "nothing after the close")

    def test_a_server_with_no_list_leaves_the_value_alone(self):
        adapter, sink, _sid = self.started()
        self.assertIsNone(sink.wait_for(
            lambda e: e["type"] == "session.options"
            and "choices" in e["data"]["options"]["model"], timeout=0.5))
        self.assertTrue(adapter.capabilities.can_set("model"))


class TestCodexListAfterStart(_CodexCase):
    def test_the_servers_models_arrive_after_the_start(self):
        _adapter, sink, _sid = self.started("models")
        found = sink.wait_for(lambda e: e["type"] == "session.options"
                              and "choices" in e["data"]["options"]["model"])
        self.assertIsNotNone(found)
        self.assertEqual(found["data"]["options"]["model"]["choices"], [
            {"id": "gpt-a", "label": "GPT A"},
            {"id": "gpt-b", "label": "GPT B"}])

    def test_a_slow_list_does_not_hold_the_start(self):
        started = time.monotonic()
        _adapter, sink, _sid = self.started("slow-models")
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertNotIn("choices", _options(sink.events)[0]["model"])
        self.assertIsNotNone(sink.wait_for(
            lambda e: e["type"] == "session.options"
            and "choices" in e["data"]["options"]["model"], timeout=10.0))

    def test_a_list_that_arrives_for_a_closed_adapter_is_dropped(self):
        adapter, sink, _sid = self.started()
        seen: list[int] = []

        def call(message, *a, **kw):
            adapter.close()  # closed while the list was on its way
            seen.append(len(sink.events))
            return {"data": [{"id": "gpt-a", "displayName": "GPT A"}]}

        adapter._call = call
        adapter._fetch_models()
        self.assertEqual(len(seen), 1)
        self.assertEqual(sink.events[seen[0]:], [], "nothing after the close")

    def test_a_refused_list_leaves_the_value_alone(self):
        _adapter, sink, _sid = self.started()
        self.assertIsNone(sink.wait_for(
            lambda e: e["type"] == "session.options"
            and "choices" in e["data"]["options"]["model"], timeout=0.5))


class TestAcpPlanModeOff(_AcpAdapterCase):
    """Plan mode off goes back to the mode from before it, else to the
    agent's ``default``, never to the first mode it lists (G32)."""

    def test_an_agent_listing_yolo_first_is_not_sent_to_it(self):
        adapter, _sink, _sid = self.started("yolo-first")
        self.assertEqual(adapter._state.mode, "plan")
        with self.assertRaises(AdapterError) as ctx:
            adapter.set_option("plan_mode", False)
        self.assertEqual(ctx.exception.code, "refused")
        modes = [m["params"]["modeId"] for m in self.sent()
                 if m.get("method") == "session/set_mode"]
        self.assertEqual(modes, [], "nothing is sent")

    def test_with_no_mode_from_before_it_goes_to_default(self):
        adapter, _sink, _sid = self.started()
        adapter._mode_before = ""
        adapter.set_option("plan_mode", True)
        adapter._mode_before = ""
        adapter.set_option("plan_mode", False)
        modes = [m["params"]["modeId"] for m in self.sent()
                 if m.get("method") == "session/set_mode"]
        self.assertEqual(modes[-1], "default")


class TestSteerEffect(unittest.TestCase):
    """``caps.steer_effect``: what a steer does to the turn in progress,
    from each adapter's steer recordings; absent where nothing proves
    it."""

    def caps(self, events: list[dict]) -> dict:
        return [e for e in events if e["type"] == "session.caps"][-1][
            "data"]["caps"]

    def test_claude_cuts_the_reply(self):
        caps = self.caps(claude_wire.replay(_lines(
            "claude_stream", "2.1.328", "steer")))
        self.assertEqual((caps["steer"], caps["steer_effect"]),
                         (True, "stops_reply"))

    def test_codex_cuts_the_reply(self):
        caps = self.caps(codex_wire.replay(_lines(
            "codex_appserver", "0.170.0", "steer")))
        self.assertEqual((caps["steer"], caps["steer_effect"]),
                         (True, "stops_reply"))

    def test_opencode_waits_for_the_step(self):
        caps = self.caps(opencode_wire.replay(_lines(
            "opencode_serve", "2.3.2", "steer")))
        self.assertEqual((caps["steer"], caps["steer_effect"]),
                         (True, "after_tool"))

    def test_acp_has_no_steer_and_no_effect(self):
        caps = self.caps(acp.replay(_lines("acp", "qwen-code-0.28.0",
                                           "hello")))
        self.assertFalse(caps["steer"])
        self.assertNotIn("steer_effect", caps)

    def test_no_steer_no_effect(self):
        # Claude before an init lists interrupt_send_now_v1: steer off.
        narrowed = claude_wire.PROVEN.narrowed(Caps(options=frozenset()))
        self.assertEqual(narrowed.steer_effect, "")
        self.assertNotIn("steer_effect", narrowed.as_dict())
        self.assertEqual(Caps(steer=True).narrowed(claude_wire.PROVEN)
                         .steer_effect, "stops_reply")
        with self.assertRaises(ValueError):
            Caps(steer=True, steer_effect="maybe")

    def test_one_set_of_values(self):
        from swe.remote.adapters import base
        self.assertEqual(base.STEER_EFFECTS, ev.STEER_EFFECTS)


class TestOptionsView(unittest.TestCase):
    def test_only_what_the_caps_allow_and_only_on_a_change(self):
        view = opts.OptionsView()
        offered = {"model": opts.string_option("a", opts.UNVERIFIED),
                   "plan_mode": opts.flag_option(False, opts.UNVERIFIED)}
        caps = Caps(options=frozenset({"model"}))
        first = view.changed("k", offered, caps, 1.0)
        self.assertEqual(set(first["data"]["options"]), {"model"})
        self.assertIsNone(view.changed("k", offered, caps, 2.0))
        # Nothing to offer, never offered: no event at all.
        self.assertIsNone(opts.OptionsView().changed("k", offered, Caps(), 1))


# ---------------------------------------------------------------------
# The keeper keeps the value true


CAPS = Caps(plan_mode=True, options=frozenset({"model", "permission_mode"}))


class TestKeeper(KeeperCase):
    def plan(self, **kw) -> dict:
        return {"caps": CAPS, "choices": {"model": ["sonnet", "haiku"]},
                **kw}

    def test_the_options_at_start_in_the_log_the_welcome_and_status(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        shown = feed.event("session.options")["data"]["options"]
        self.assertEqual(set(shown), {"model", "permission_mode",
                                      "plan_mode"})
        self.assertEqual([c["id"] for c in shown["model"]["choices"]],
                         ["sonnet", "haiku"])
        self.assertEqual(feed.client.welcome["options"], shown)
        self.assertEqual(self.status(feed)["options"], shown)

    def test_a_change_is_logged_before_its_result(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        result = feed.result("m")
        self.assertTrue(result["data"]["ok"])
        changed = [e for e in feed.events("session.options")
                   if e["data"]["options"]["model"]["value"] == "haiku"]
        self.assertEqual(len(changed), 1)
        self.assertLess(changed[0]["seq"], result["seq"])
        self.assertEqual(self.status(feed)["options"]["model"]["value"],
                         "haiku")

    def test_a_refused_change_leaves_the_value_and_says_why(self):
        keeper = self.keeper(self.plan(refuse_options={
            "model": "Model 'haiku' is not available"}))
        feed = self.connect(keeper)
        feed.live()
        before = len(feed.events("session.options"))
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        result = feed.result("m")
        self.assertEqual((result["data"]["ok"], result["data"]["note"]),
                         (False, "Model 'haiku' is not available"))
        self.assertEqual(len(feed.events("session.options")), before)

    def test_the_agent_changing_its_own_option_is_logged(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        self.adapters[0].agent_sets("plan_mode", True)
        shown = feed.until(
            lambda f: f["t"] == "event"
            and f["e"]["type"] == "session.options"
            and f["e"]["data"]["options"]["plan_mode"]["value"] is True,
            "plan mode on")["e"]
        self.assertTrue(shown["data"]["options"]["plan_mode"]["value"])

    def test_a_change_while_parked_shows_what_the_next_start_uses(self):
        keeper = self.keeper(self.plan(), self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("park", command_id="p")
        feed.result("p")
        feed.client.request("set_option", command_id="m", name="model",
                            value="sonnet")
        result = feed.result("m")
        self.assertEqual(result["data"]["note"],
                         "applies when the agent starts")
        shown = feed.events("session.options")[-1]["data"]["options"]
        self.assertEqual(shown["model"]["value"], "sonnet")
        self.assertLess(feed.events("session.options")[-1]["seq"],
                        result["seq"])
        # The next start says what the agent made of it.
        self.send(feed)
        feed.event("turn.ended")
        self.assertEqual(self.adapters[1].spec.model, "sonnet")
        self.assertEqual(self.status(feed)["options"]["model"]["value"],
                         "sonnet")

    def test_choosing_a_permission_mode_while_parked_leaves_plan_mode(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        self.adapters[0].agent_sets("plan_mode", True)
        feed.client.request("park", command_id="p")
        feed.result("p")
        feed.client.request("set_option", command_id="m",
                            name="permission_mode", value="default")
        feed.result("m")
        shown = self.status(feed)["options"]
        self.assertEqual((shown["permission_mode"]["value"],
                          shown["plan_mode"]["value"]), ("default", False))

    def test_while_parked_plan_mode_and_the_plan_permission_mode_agree(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        self.adapters[0].agent_sets("permission_mode", "default")
        feed.client.request("park", command_id="p")
        feed.result("p")

        def shown():
            options = self.status(feed)["options"]
            return (options["permission_mode"]["value"],
                    options["plan_mode"]["value"])

        for cid, name, value, want in (
                ("a", "permission_mode", "plan", ("plan", True)),
                ("b", "permission_mode", "default", ("default", False)),
                ("c", "plan_mode", True, ("plan", True)),
                ("d", "plan_mode", False, ("default", False))):
            feed.client.request("set_option", command_id=cid, name=name,
                                value=value)
            feed.result(cid)
            self.assertEqual(shown(), want, (name, value))

    def test_a_new_keeper_reads_the_options_back_from_the_log(self):
        keeper = self.keeper(self.plan())
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("set_option", command_id="m", name="model",
                            value="haiku")
        feed.result("m")
        kill(keeper)
        feed.closed()
        refuse = {"on": True}
        make = self.factory

        def failing():
            if refuse["on"]:
                raise AdapterError("claude did not start", code="agent_down")
            return make()

        self.factory = failing
        second = self.keeper()
        feed = self.connect(second)
        feed.live()
        # The agent is down, and the welcome still says what was in
        # force: the log's last session.options.
        self.assertEqual(feed.client.welcome["agent"], "down")
        self.assertEqual(feed.client.welcome["options"]["model"]["value"],
                         "haiku")

    def test_a_start_whose_list_has_not_come_keeps_the_last_list(self):
        # The second agent says its value before (or without) its list,
        # as Codex and opencode do now.
        keeper = self.keeper(self.plan(), {"caps": CAPS})
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed)
        feed.event("turn.ended")
        shown = self.status(feed)["options"]["model"]
        self.assertEqual([c["id"] for c in shown["choices"]],
                         ["sonnet", "haiku"])

    def test_an_agent_with_narrower_caps_offers_less(self):
        keeper = self.keeper(self.plan(), {"caps": Caps()})
        feed = self.connect(keeper)
        feed.live()
        feed.client.request("park", command_id="p")
        feed.result("p")
        self.send(feed)
        feed.event("turn.ended")
        self.assertEqual(self.status(feed)["options"], {})


# ---------------------------------------------------------------------
# The hub


class TestHub(V2Case):
    def test_status_lists_the_feature(self):
        status, body = self.call("GET", "/v1/status")
        self.assertIn(protocol.FEATURE_SESSION_OPTIONS, body["features"])

    def test_the_snapshot_and_the_stream_carry_the_options(self):
        self.plans.append({"choices": {"model": ["sonnet", "haiku"]}})
        key = self.launch()
        self.drain(key)
        body = self.snapshot(key)
        self.assertEqual(set(body["options"]), {"model", "plan_mode"})
        self.assertEqual(body["options"]["model"]["value"], "")
        status, out = self.call("POST", f"/v1/sessions/{key}/command", {
            "op": "set_option", "name": "model", "value": "haiku",
            "command_id": "w-1"})
        self.assertEqual((status, out["receipt"]), (200, "accepted"))
        self.feed.result("w-1")
        self.drain(key)
        body = self.snapshot(key)
        self.assertEqual(body["options"]["model"]["value"], "haiku")
        # A v2 stream replays the change as an event (not a timeline
        # item: the snapshot's messages leave it out).
        self.assertNotIn("session.options",
                         [m["type"] for m in body["messages"]])
        frames = self.caught_up(self.sse(key, last=1, epoch=body["epoch"]))
        self.assertIn("session.options", [f["type"] for f in frames])

    def test_a_kept_session_reads_the_keepers_copy(self):
        # The runtime's copy (from the keeper's welcome and its events)
        # wins over the journal's fold: the journal is a ring that can
        # have dropped the last session.options of a long session.
        self.plans.append({"choices": {"model": ["sonnet"]}})
        key = self.launch()
        self.drain(key)
        owned = self.srv.state.runtime.session(key)
        self.assertEqual(owned.offered, self.snapshot(key)["options"])
        owned.offered = {"model": opts.string_option("kept", opts.UNVERIFIED)}
        self.assertEqual(self.snapshot(key)["options"]["model"]["value"],
                         "kept")

    def test_a_refused_option_is_answered_with_the_reason(self):
        key = self.launch()
        self.drain(key)
        status, out = self.call("POST", f"/v1/sessions/{key}/command", {
            "op": "set_option", "name": "effort", "value": "high",
            "command_id": "w-2"})
        self.assertEqual((status, out["error"]["code"]),
                         (409, "command_rejected"))


class TestFold(unittest.TestCase):
    def test_a_session_no_keeper_here_holds_reads_the_journal(self):
        # A satellite keeper's session: the hub's copy of its journal
        # is all there is, so the snapshot folds it.
        fold = sync_mod.Fold()
        shown = {"model": opts.string_option("x", opts.NEXT_TURN)}
        fold.add(ev.session_options("k", shown))
        self.assertEqual(fold.view()["options"], shown)
        self.assertIn("session.options", sync_mod.BOOKKEEPING_TYPES)


if __name__ == "__main__":
    unittest.main()
